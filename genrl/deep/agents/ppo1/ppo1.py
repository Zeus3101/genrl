from typing import Any, Dict, Tuple, Union

import gym
import numpy as np
import torch
import torch.nn as nn
import torch.optim as opt

from ....environments import VecEnv
from ...common import RolloutBuffer, get_env_properties, get_model, safe_mean
from ..base import OnPolicyAgent


class PPO1(OnPolicyAgent):
    """
    Proximal Policy Optimization algorithm (Clipped policy).

    Paper: https://arxiv.org/abs/1707.06347

    :param network_type: The deep neural network layer types ['mlp']
    :param env: The environment to learn from
    :param timesteps_per_actorbatch: timesteps per actor per update
    :param gamma: discount factor
    :param clip_param: clipping parameter epsilon
    :param actor_batchsize: trajectories per optimizer epoch
    :param epochs: the optimizer's number of epochs
    :param lr_policy: policy network learning rate
    :param lr_value: value network learning rate
    :param policy_copy_interval: number of optimizer before copying
        params from new policy to old policy
    :param save_interval: Number of episodes between saves of models
    :param seed: seed for torch and gym
    :param device: device to use for tensor operations; 'cpu' for cpu
        and 'cuda' for gpu
    :param run_num: if model has already been trained
    :param save_model: directory the user wants to save models to
    :param load_model: model loading path
    :type network_type: str
    :type env: Gym environment
    :type timesteps_per_actorbatch: int
    :type gamma: float
    :type clip_param: float
    :type actor_batchsize: int
    :type epochs: int
    :type lr_policy: float
    :type lr_value: float
    :type policy_copy_interval: int
    :type save_interval: int
    :type seed: int
    :type device: string
    :type run_num: boolean
    :type save_model: string
    :type load_model: string
    :type rollout_size: int
    """

    def __init__(
        self,
        network_type: str,
        env: Union[gym.Env, VecEnv],
        batch_size: int = 256,
        gamma: float = 0.99,
        clip_param: float = 0.2,
        epochs: int = 1000,
        lr_policy: float = 0.001,
        lr_value: float = 0.001,
        layers: Tuple = (64, 64),
        rollout_size: int = 2048,
        **kwargs
    ):

        super(PPO1, self).__init__(
            network_type,
            env,
            batch_size,
            layers,
            gamma,
            lr_policy,
            lr_value,
            epochs,
            rollout_size,
            **kwargs
        )

        self.clip_param = clip_param
        self.entropy_coeff = kwargs.get("entropy_coeff", 0.01)
        self.value_coeff = kwargs.get("value_coeff", 0.5)

        self.empty_logs()
        self.create_model()

    def create_model(self) -> None:
        """
        Creates actor critic model and initialises optimizers
        """
        # Instantiate networks and optimizers
        state_dim, action_dim, discrete, action_lim = get_env_properties(self.env)

        self.ac = get_model("ac", self.network_type)(
            state_dim,
            action_dim,
            self.layers,
            "V",
            discrete=discrete,
            action_lim=action_lim,
        ).to(self.device)

        # load paramaters if already trained
        if self.load_model is not None:
            self.load(self)
            self.ac.actor.load_state_dict(self.checkpoint["policy_weights"])
            self.ac.critic.load_state_dict(self.checkpoint["value_weights"])
            for key, item in self.checkpoint.items():
                if key not in ["policy_weights", "value_weights", "save_model"]:
                    setattr(self, key, item)
            print("Loaded pretrained model")

        self.optimizer_policy = opt.Adam(self.ac.actor.parameters(), lr=self.lr_policy)
        self.optimizer_value = opt.Adam(self.ac.critic.parameters(), lr=self.lr_value)

        self.rollout = RolloutBuffer(
            self.rollout_size,
            self.env.observation_space,
            self.env.action_space,
            n_envs=self.env.n_envs,
        )

    def select_action(self, state: np.ndarray) -> np.ndarray:
        """
        Selection of action

        :param state: Observation state
        :type state: int, float, ...
        :returns: Action based on the state and epsilon value
        :rtype: int, float, ...
        """
        state = torch.as_tensor(state).float().to(self.device)
        # create distribution based on actor output
        action, c_new = self.ac.get_action(state, deterministic=False)
        value = self.ac.get_value(state)

        return action.detach().cpu().numpy(), value, c_new.log_prob(action)

    def evaluate_actions(self, old_states, old_actions):
        """
        Evaluate the performance of older actions

        :param old_states: Previous states
        :param old_actions: Previous actions
        :type old_states: NumPy Array
        :type old_actions: NumPy Array
        :returns: Value, Log Probabilities of old actions, Entropy
        """
        value = self.ac.get_value(old_states)
        _, dist = self.ac.get_action(old_states)
        return value, dist.log_prob(old_actions), dist.entropy()

    # get clipped loss for single trajectory (episode)
    def get_traj_loss(self, values: np.ndarray, dones: bool):
        """
        (Get trajectory of agent to calculate discounted rewards and
calculate losses)

        :param value: Value of a state
        :param done: True if the state is terminal, else False
        :type value: NumPy Array
        :type done: boolean
        """
        self.rollout.compute_returns_and_advantage(
            values.detach().cpu().numpy(), dones, use_gae=True
        )

    def update_policy(self):
        """
        Function to calculate loss from rollouts and update the policy
        """
        for rollout in self.rollout.get(self.batch_size):
            actions = rollout.actions

            if isinstance(self.env.action_space, gym.spaces.Discrete):
                actions = actions.long().flatten()

            values, log_prob, entropy = self.evaluate_actions(
                rollout.observations, actions
            )

            advantages = rollout.advantages
            advantages = (advantages - advantages.mean()) / (advantages.std() + 1e-8)

            ratio = torch.exp(log_prob - rollout.old_log_prob)

            policy_loss_1 = advantages * ratio
            policy_loss_2 = advantages * torch.clamp(
                ratio, 1 - self.clip_param, 1 + self.clip_param
            )
            policy_loss = -torch.min(policy_loss_1, policy_loss_2).mean()
            self.logs["policy_loss"].append(policy_loss.item())

            values = values.flatten()

            value_loss = nn.functional.mse_loss(rollout.returns, values)
            self.logs["value_loss"].append(torch.mean(value_loss).item())

            entropy_loss = -torch.mean(entropy)  # Change this to entropy
            self.logs["policy_entropy"].append(entropy_loss.item())

            loss = (
                policy_loss + self.entropy_coeff * entropy_loss
            )  # + self.vf_coef * value_loss

            self.optimizer_policy.zero_grad()
            loss.backward()
            torch.nn.utils.clip_grad_norm_(self.ac.actor.parameters(), 0.5)
            self.optimizer_policy.step()

            self.optimizer_value.zero_grad()
            value_loss.backward()
            torch.nn.utils.clip_grad_norm_(self.ac.critic.parameters(), 0.5)
            self.optimizer_value.step()

    def get_hyperparams(self) -> Dict[str, Any]:
        """
        Loads important hyperparameters that need to be loaded or saved

        :returns: Hyperparameters that need to be saved or loaded
        :rtype: dict
        """
        hyperparams = {
            "network_type": self.network_type,
            "batch_size": self.batch_size,
            "gamma": self.gamma,
            "clip_param": self.clip_param,
            "lr_policy": self.lr_policy,
            "lr_value": self.lr_value,
            "policy_weights": self.ac.actor.state_dict(),
            "value_weights": self.ac.critic.state_dict(),
            "rollout_size": self.rollout_size,
        }

        return hyperparams

    def get_logging_params(self) -> Dict[str, Any]:
        """
        :returns: Logging parameters for monitoring training
        :rtype: dict
        """

        logs = {
            "policy_loss": safe_mean(self.logs["policy_loss"]),
            "value_loss": safe_mean(self.logs["value_loss"]),
            "policy_entropy": safe_mean(self.logs["policy_entropy"]),
            "mean_reward": safe_mean(self.rewards),
        }

        self.empty_logs()
        return logs

    def empty_logs(self):
        """
        Empties logs
        """
        self.logs = {}
        self.logs["policy_loss"] = []
        self.logs["value_loss"] = []
        self.logs["policy_entropy"] = []
        self.rewards = []
