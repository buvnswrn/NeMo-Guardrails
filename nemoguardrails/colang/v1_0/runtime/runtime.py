# SPDX-FileCopyrightText: Copyright (c) 2023 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
# http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

import inspect
import logging
from textwrap import indent
from typing import Any, Dict, List, Tuple
from urllib.parse import urljoin

import aiohttp
from langchain.chains.base import Chain

from nemoguardrails.actions.actions import ActionResult
from nemoguardrails.colang import parse_colang_file
from nemoguardrails.colang.runtime import Runtime
from nemoguardrails.colang.v1_0.runtime.flows import compute_context, compute_next_steps
from nemoguardrails.utils import new_event_dict

log = logging.getLogger(__name__)


class RuntimeV1_0(Runtime):
    """Runtime for executing the guardrails."""

    async def generate_events(self, events: List[dict]) -> List[dict]:
        """Generates the next events based on the provided history.

        This is a wrapper around the `process_events` method, that will keep
        processing the events until the `listen` event is produced.

        :return: The list of events.
        """
        events = events.copy()
        new_events = []

        while True:
            last_event = events[-1]

            log.info("Processing event: %s", last_event)

            event_type = last_event["type"]
            log.info(
                "Event :: %s %s",
                event_type,
                str({k: v for k, v in last_event.items() if k != "type"}),
            )

            # If we need to execute an action, we start doing that.
            if last_event["type"] == "StartInternalSystemAction":
                next_events = await self._process_start_action(events)

            # If we need to start a flow, we parse the content and register it.
            elif last_event["type"] == "start_flow":
                next_events = await self._process_start_flow(events)

            else:
                # We need to slide all the flows based on the current event,
                # to compute the next steps.
                next_events = await self._compute_next_steps(events)

                if len(next_events) == 0:
                    next_events = [new_event_dict("Listen")]

            # Otherwise, we append the event and continue the processing.
            events.extend(next_events)
            new_events.extend(next_events)

            # If the next event is a listen, we stop the processing.
            if next_events[-1]["type"] == "Listen":
                break

            # As a safety measure, we stop the processing if we have too many events.
            if len(new_events) > 100:
                raise Exception("Too many events.")

        return new_events

    async def _compute_next_steps(self, events: List[dict]) -> List[dict]:
        """Computes the next step based on the current flow."""
        next_steps = compute_next_steps(events, self.flow_configs)

        # If there are any StartInternalSystemAction events, we mark if they are system actions or not
        for event in next_steps:
            if event["type"] == "StartInternalSystemAction":
                is_system_action = False
                fn = self.action_dispatcher.get_action(event["action_name"])
                if fn:
                    action_meta = getattr(fn, "action_meta", {})
                    is_system_action = action_meta.get("is_system_action", False)
                event["is_system_action"] = is_system_action

        return next_steps

    @staticmethod
    def _internal_error_action_result(message: str):
        """Helper to construct an action result for an internal error."""
        return ActionResult(
            events=[
                {
                    "type": "BotIntent",
                    "intent": "inform internal error occurred",
                },
                {
                    "type": "StartUtteranceBotAction",
                    "script": message,
                },
                # We also want to hide this from now from the history moving forward
                {"type": "hide_prev_turn"},
            ]
        )

    async def _process_start_action(self, events: List[dict]) -> List[dict]:
        """Starts the specified action, waits for it to finish and posts back the result."""

        event = events[-1]

        action_name = event["action_name"]
        action_params = event["action_params"]
        action_result_key = event["action_result_key"]
        action_uid = event["action_uid"]

        context = {}
        action_meta = {}

        fn = self.action_dispatcher.get_action(action_name)

        # TODO: check action is available in action server
        if fn is None:
            status = "failed"
            result = self._internal_error_action_result(
                f"Action '{action_name}' not found."
            )

        else:
            context = compute_context(events)

            # We pass all the parameters that are passed explicitly to the action.
            kwargs = {**action_params}

            action_meta = getattr(fn, "action_meta", {})

            parameters = []
            action_type = "class"

            if inspect.isfunction(fn) or inspect.ismethod(fn):
                # We also add the "special" parameters.
                parameters = inspect.signature(fn).parameters
                action_type = "function"

            elif isinstance(fn, Chain):
                # If we're dealing with a chain, we list the annotations
                # TODO: make some additional type checking here
                parameters = fn.input_keys
                action_type = "chain"

            # For every parameter that start with "__context__", we pass the value
            for parameter_name in parameters:
                if parameter_name.startswith("__context__"):
                    var_name = parameter_name[11:]
                    kwargs[parameter_name] = context.get(var_name)

            # If there are parameters which are variables, we replace with actual values.
            for k, v in kwargs.items():
                if isinstance(v, str) and v.startswith("$"):
                    var_name = v[1:]
                    if var_name in context:
                        kwargs[k] = context[var_name]

            # If we have an action server, we use it for non-system/non-chain actions
            if (
                self.config.actions_server_url
                and not action_meta.get("is_system_action")
                and action_type != "chain"
            ):
                result, status = await self._get_action_resp(
                    action_meta, action_name, kwargs
                )
            else:
                # We don't send these to the actions server;
                # TODO: determine if we should
                if "events" in parameters:
                    kwargs["events"] = events

                if "context" in parameters:
                    kwargs["context"] = context

                if "config" in parameters:
                    kwargs["config"] = self.config

                if "llm_task_manager" in parameters:
                    kwargs["llm_task_manager"] = self.llm_task_manager

                # Add any additional registered parameters
                for k, v in self.registered_action_params.items():
                    if k in parameters:
                        kwargs[k] = v

                if (
                    "llm" in kwargs
                    and f"{action_name}_llm" in self.registered_action_params
                ):
                    kwargs["llm"] = self.registered_action_params[f"{action_name}_llm"]

                log.info("Executing action :: %s", action_name)
                result, status = await self.action_dispatcher.execute_action(
                    action_name, kwargs
                )

            # If the action execution failed, we return a hardcoded message
            if status == "failed":
                # TODO: make this message configurable.
                result = self._internal_error_action_result(
                    "I'm sorry, an internal error has occurred."
                )

        return_value = result
        return_events = []
        context_updates = {}

        if isinstance(result, ActionResult):
            return_value = result.return_value
            return_events = result.events
            context_updates.update(result.context_updates)

        # If we have an action result key, we also record the update.
        if action_result_key:
            context_updates[action_result_key] = return_value

        next_steps = []

        if context_updates:
            # We check if at least one key changed
            changes = False
            for k, v in context_updates.items():
                if context.get(k) != v:
                    changes = True
                    break

            if changes:
                next_steps.append(new_event_dict("ContextUpdate", data=context_updates))

        next_steps.append(
            new_event_dict(
                "InternalSystemActionFinished",
                action_uid=action_uid,
                action_name=action_name,
                action_params=action_params,
                action_result_key=action_result_key,
                status=status,
                is_success=status != "failed",
                failure_reason=status,
                return_value=return_value,
                events=return_events,
                is_system_action=action_meta.get("is_system_action", False),
            )
        )

        # If the action returned additional events, we also add them to the next steps.
        if return_events:
            next_steps.extend(return_events)

        return next_steps

    async def _get_action_resp(
        self, action_meta: Dict[str, Any], action_name: str, kwargs: Dict[str, Any]
    ) -> Tuple[Dict[str, Any], str]:
        """Interact with actions and get response from action-server and system actions."""
        result, status = {}, "failed"  # default response
        try:
            # Call the Actions Server if it is available.
            # But not for system actions, those should still run locally.
            if (
                action_meta.get("is_system_action", False)
                or self.config.actions_server_url is None
            ):
                result, status = await self.action_dispatcher.execute_action(
                    action_name, kwargs
                )
            else:
                url = urljoin(
                    self.config.actions_server_url, "/v1/actions/run"
                )  # action server execute action path
                data = {"action_name": action_name, "action_parameters": kwargs}
                async with aiohttp.ClientSession() as session:
                    try:
                        async with session.post(url, json=data) as resp:
                            if resp.status != 200:
                                raise ValueError(
                                    f"Got status code {resp.status} while getting response from {action_name}"
                                )

                            resp = await resp.json()
                            result, status = resp.get("result", result), resp.get(
                                "status", status
                            )
                    except Exception as e:
                        log.info(f"Exception {e} while making request to {action_name}")
                        return result, status

        except Exception as e:
            log.info(f"Failed to get response from {action_name} due to exception {e}")
        return result, status

    async def _process_start_flow(self, events: List[dict]) -> List[dict]:
        """Starts a flow."""

        event = events[-1]

        flow_id = event["flow_id"]

        # Up to this point, the body will be the sequence of instructions.
        # We need to alter it to be an actual flow definition, i.e., add `define flow xxx`
        # and intent the body.
        body = event["flow_body"]
        body = "define flow " + flow_id + ":\n" + indent(body, "  ")

        # We parse the flow
        parsed_data = parse_colang_file("dynamic.co", content=body)

        assert len(parsed_data["flows"]) == 1
        flow = parsed_data["flows"][0]

        # To make sure that the flow will start now, we add a start_flow element at
        # the beginning as well.
        flow["elements"].insert(0, {"_type": "start_flow", "flow_id": flow_id})

        # We add the flow to the list of flows.
        self._load_flow_config(flow)

        # And we compute the next steps. The new flow should match the current event,
        # and start.
        next_steps = await self._compute_next_steps(events)

        return next_steps
