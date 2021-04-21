# coding=utf-8
# Copyright 2021 DeepMind Technologies Limited.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Android environment implementation."""

import copy

from typing import Any, Dict
from absl import logging
from android_env.components import action_type
from android_env.components import base_simulator
from android_env.components import coordinator as coordinator_lib
from android_env.components import specs
from android_env.components import task_manager as task_manager_lib
from android_env.proto import task_pb2
import dm_env
import numpy as np


StepType = dm_env.StepType


class AndroidEnv(dm_env.Environment):
  """An RL environment that interacts with Android apps."""

  def __init__(
      self,
      simulator: base_simulator.BaseSimulator,
      task: task_pb2.Task,
      task_manager: task_manager_lib.TaskManager,
      coordinator: coordinator_lib.Coordinator,
  ):
    """Initializes the state of this AndroidEnv object."""

    self._simulator = simulator
    self._task = task
    self._task_manager = task_manager
    self._coordinator = coordinator
    self._is_closed = False
    self._latest_action = {}
    self._latest_observation = {}
    self._latest_extras = {}
    self._latest_step_type = StepType.LAST
    self._reset_next_step = True

    # Logging settings
    self._log_dict = {
        'restart_count': 0,  # Counts unexpected simulator restarts.
        'reset_count_step_timeout': 0,
    }
    self._log_prefixes = ['androidenv_total', 'androidenv_episode']
    for prefix in self._log_prefixes:
      self._log_dict[f'{prefix}_steps'] = 0.0
      for act_type in action_type.ActionType:
        self._log_dict[f'{prefix}_action_type_{act_type.name}'] = 0.0

    # Log init info
    logging.info('Task config: %s', self._task)
    logging.info('Action spec: %s', self.action_spec())
    logging.info('Observation spec: %s', self.observation_spec())
    logging.info('Task extras spec: %s', self.task_extras_spec())

  def action_spec(self) -> Dict[str, dm_env.specs.Array]:
    return specs.base_action_spec()

  def observation_spec(self) -> Dict[str, dm_env.specs.Array]:
    return specs.base_observation_spec(self._simulator.screen_dimensions())

  def task_extras_spec(self) -> Dict[str, dm_env.specs.Array]:
    return specs.task_extras_spec(task=self._task)

  @property
  def raw_action(self):
    return self._latest_action

  @property
  def raw_observation(self):
    return self._latest_observation

  def reset(self) -> dm_env.TimeStep:
    """Reset the environment."""

    logging.info('Resetting AndroidEnv.')
    self._coordinator.reset()

    # Reset relevant values
    self._latest_action = {}
    self._reset_log_dict()

    # Fetch observation and task_extras
    observation = self._coordinator.execute_action(action=None)
    task_extras = self._task_manager.get_current_extras()
    if observation is not None:
      self._latest_observation = observation.copy()
    self._latest_extras = task_extras.copy()

    self._reset_next_step = False
    self._latest_step_type = StepType.FIRST

    logging.info('Done resetting AndroidEnv.')
    logging.info('************* NEW EPISODE *************')

    return dm_env.TimeStep(
        step_type=self._latest_step_type,
        observation=self._latest_observation,
        reward=0.0,
        discount=0.0)

  def step(self, action: Dict[str, np.ndarray]) -> dm_env.TimeStep:
    """Take a step in the environment."""
    self._latest_action = action.copy()

    # Check if the simulation has to be restarted
    if self._coordinator.should_restart():
      self._log_dict['restart_count'] += 1
      self._coordinator.restart_simulator()
      self._reset_next_step = True
      self._latest_step_type = StepType.LAST
      return dm_env.termination(
          observation=self._latest_observation, reward=0.0)

    if self._coordinator.check_timeout():
      self._log_dict['reset_count_step_timeout'] += 1
      logging.info('Step has timed out. Ending episode.')
      self._reset_next_step = True
      self._latest_step_type = StepType.LAST
      return dm_env.termination(
          observation=self._latest_observation, reward=0.0)

    # Check if it's time to reset the episode
    if self._reset_next_step:
      return self.reset()

    self._update_log_dict(act_type=action['action_type'].item())
    self._task_manager.increment_steps()

    # Fetch observation, reward and task_extras.
    observation = self._coordinator.execute_action(action)
    reward = self._task_manager.get_current_reward()
    task_extras = self._task_manager.get_current_extras()
    if observation is not None:
      self._latest_observation = observation.copy()
    self._latest_extras = task_extras.copy()

    # Determine step type
    self._reset_next_step = self._task_manager.check_if_episode_ended()
    step_type = StepType.LAST if self._reset_next_step else StepType.MID
    self._latest_step_type = step_type

    # Return timestep with reward and observation just computed
    return dm_env.TimeStep(
        step_type=self._latest_step_type,
        observation=self._latest_observation,
        reward=reward,
        discount=0.0 if self._reset_next_step else 1.0)

  def task_extras(self, latest_only: bool = True) -> Dict[str, np.ndarray]:
    """Return latest task extras."""

    task_extras = {}
    for key, spec in self.task_extras_spec().items():
      if key in self._latest_extras:
        extra_values = self._latest_extras[key].astype(spec.dtype)
        for extra in extra_values:
          spec.validate(extra)
        task_extras[key] = extra_values[-1] if latest_only else extra_values
    return task_extras

  def android_logs(self) -> Dict[str, Any]:
    """Expose internal counter values."""
    return self._flush_log_dict()

  def _update_log_dict(self, act_type: int) -> None:
    """Increment internal counters."""

    act_type = action_type.ActionType(act_type)
    for prefix in self._log_prefixes:
      self._log_dict[f'{prefix}_steps'] += 1
      self._log_dict[f'{prefix}_action_type_{act_type.name}'] += 1

  def _flush_log_dict(self) -> Dict[str, Any]:
    """Return internal counter values."""

    log_dict = copy.deepcopy(self._log_dict)
    log_dict.update(self._coordinator.log_dict())
    for prefix in self._log_prefixes:
      if log_dict[f'{prefix}_steps'] == 0:
        logging.warning('%s_steps is 0. Skipping ratio logs.', prefix)
        continue
      for act_type in action_type.ActionType:
        log_dict[f'{prefix}_action_type_ratio_{act_type.name}'] = log_dict[
            f'{prefix}_action_type_{act_type.name}'] / log_dict[
                f'{prefix}_steps']

    return log_dict

  def _reset_log_dict(self) -> None:
    """Reset internal counter values."""

    for key in self._log_dict:
      if key.startswith('androidenv_episode'):
        self._log_dict[key] = 0.0

  def close(self) -> None:
    """Clean up running processes, threads and local files."""

    logging.info('Cleaning up AndroidEnv...')
    if hasattr(self, '_coordinator'):
      self._coordinator.close()
    self._is_closed = True
    logging.info('Done cleaning up AndroidEnv.')

  def __del__(self) -> None:
    if not self._is_closed:
      self.close()
