#!/usr/bin/env python3

# docs and experiment results can be found at https://docs.cleanrl.dev/rl-algorithms/ddpg/#ddpg_continuous_actionpy
import argparse
import os
import random
import time
from distutils.util import strtobool

import gym
import numpy as np
# import pybullet_envs  # noqa
import torch
import torch.nn as nn
import torch.nn.functional as F
import torch.optim as optim
from stable_baselines3.common.buffers import ReplayBuffer
from torch.utils.tensorboard import SummaryWriter

from torch.func import functional_call, vmap, grad

def l1_params(model):
    s = 0
    total_dims = 0
    for p in model.parameters():
        dims = p.size()
        dims = np.array(dims).prod()
        total_dims += dims
        n = p.cpu().data.numpy().reshape(-1)
        s += np.sum(np.abs(n))
    return s/total_dims

def parse_args():
    # fmt: off
    parser = argparse.ArgumentParser()
    parser.add_argument("--exp-name", type=str, default=os.path.basename(__file__).rstrip(".py"),
        help="the name of this experiment")
    parser.add_argument("--seed", type=int, default=1,
        help="seed of the experiment")
    parser.add_argument("--torch-deterministic", type=lambda x: bool(strtobool(x)), default=True, nargs="?", const=True,
        help="if toggled, `torch.backends.cudnn.deterministic=False`")
    parser.add_argument("--cuda", type=lambda x: bool(strtobool(x)), default=True, nargs="?", const=True,
        help="if toggled, cuda will be enabled by default")
    parser.add_argument("--track", type=lambda x: bool(strtobool(x)), default=False, nargs="?", const=True,
        help="if toggled, this experiment will be tracked with Weights and Biases")
    parser.add_argument("--wandb-project-name", type=str, default="cleanRL",
        help="the wandb's project name")
    parser.add_argument("--wandb-entity", type=str, default=None,
        help="the entity (team) of wandb's project")
    parser.add_argument("--capture-video", type=lambda x: bool(strtobool(x)), default=False, nargs="?", const=True,
        help="whether to capture videos of the agent performances (check out `videos` folder)")

    # Algorithm specific arguments
    parser.add_argument("--env-id", type=str, default="HopperBulletEnv-v0",
        help="the id of the environment")
    parser.add_argument("--total-timesteps", type=int, default=1000000,
        help="total timesteps of the experiments")
    parser.add_argument("--learning-rate", type=float, default=3e-4,
        help="the learning rate of the optimizer")
    parser.add_argument("--buffer-size", type=int, default=int(1e6),
        help="the replay memory buffer size")
    parser.add_argument("--gamma", type=float, default=0.99,
        help="the discount factor gamma")
    parser.add_argument("--tau", type=float, default=0.005,
        help="target smoothing coefficient (default: 0.005)")
    parser.add_argument("--batch-size", type=int, default=256,
        help="the batch size of sample from the reply memory")
    parser.add_argument("--exploration-noise", type=float, default=0.1,
        help="the scale of exploration noise")
    parser.add_argument("--learning-starts", type=int, default=25e3,
        help="timestep to start learning")
    parser.add_argument("--policy-frequency", type=int, default=2,
        help="the frequency of training policy (delayed)")
    parser.add_argument("--noise-clip", type=float, default=0.5,
        help="noise clip parameter of the Target Policy Smoothing Regularization")
    args = parser.parse_args()
    # fmt: on
    return args


def make_env(env_id, seed, idx, capture_video, run_name):
    def thunk():
        env = gym.make(env_id)
        env = gym.wrappers.RecordEpisodeStatistics(env)
        if capture_video:
            if idx == 0:
                env = gym.wrappers.RecordVideo(env, f"videos/{run_name}")
        env.seed(seed)
        env.action_space.seed(seed)
        env.observation_space.seed(seed)
        return env

    return thunk


# ALGO LOGIC: initialize agent here:
class QNetwork(nn.Module):
    def __init__(self, env):
        super().__init__()
        self.fc1 = nn.Linear(np.array(env.single_observation_space.shape).prod() + np.prod(env.single_action_space.shape), 256)
        self.fc2 = nn.Linear(256, 256)
        self.fc3 = nn.Linear(256, 1)

    def forward(self, x, a):
        x = torch.cat([x, a], 1)
        x = F.relu(self.fc1(x))
        x = F.relu(self.fc2(x))
        x = self.fc3(x)
        return x


class Actor(nn.Module):
    def __init__(self, env):
        super().__init__()
        self.fc1 = nn.Linear(np.array(env.single_observation_space.shape).prod(), 256)
        self.fc2 = nn.Linear(256, 256)
        self.fc_mu = nn.Linear(256, np.prod(env.single_action_space.shape))
        # action rescaling
        self.register_buffer(
            "action_scale", torch.tensor((env.action_space.high - env.action_space.low) / 2.0, dtype=torch.float32)
        )
        self.register_buffer(
            "action_bias", torch.tensor((env.action_space.high + env.action_space.low) / 2.0, dtype=torch.float32)
        )

    def forward(self, x):
        x = F.relu(self.fc1(x))
        x = F.relu(self.fc2(x))
        x = torch.tanh(self.fc_mu(x))
        return x * self.action_scale + self.action_bias


if __name__ == "__main__":
    args = parse_args()
    run_name = f"{args.env_id}__{args.exp_name}__{args.seed}__{int(time.time())}"
    if args.track:
        import wandb

        wandb.init(
            project=args.wandb_project_name,
            entity=args.wandb_entity,
            sync_tensorboard=True,
            config=vars(args),
            name=run_name,
            monitor_gym=True,
            save_code=True,
        )
    writer = SummaryWriter(f"runs/{run_name}")
    writer.add_text(
        "hyperparameters",
        "|param|value|\n|-|-|\n%s" % ("\n".join([f"|{key}|{value}|" for key, value in vars(args).items()])),
    )

    # TRY NOT TO MODIFY: seeding
    random.seed(args.seed)
    np.random.seed(args.seed)
    torch.manual_seed(args.seed)
    torch.backends.cudnn.deterministic = args.torch_deterministic

    device = torch.device("cuda" if torch.cuda.is_available() and args.cuda else "cpu")

    # env setup
    envs = gym.vector.SyncVectorEnv([make_env(args.env_id, args.seed, 0, args.capture_video, run_name)])
    assert isinstance(envs.single_action_space, gym.spaces.Box), "only continuous action space is supported"

    actor = Actor(envs).to(device)
    qf1 = QNetwork(envs).to(device)
    qf1_target = QNetwork(envs).to(device)
    target_actor = Actor(envs).to(device)
    target_actor.load_state_dict(actor.state_dict())
    qf1_target.load_state_dict(qf1.state_dict())
    q_optimizer = optim.Adam(list(qf1.parameters()), lr=args.learning_rate)
    actor_optimizer = optim.Adam(list(actor.parameters()), lr=args.learning_rate)

    ##
    ## RAN: m_qf1 is not used for inference, it is used to hold the momentum variables
    ##
    m_qf1 = QNetwork(envs).to(device)
    with torch.no_grad():
        for w in m_qf1.parameters():
            w.fill_(0)

    M_optimizer = optim.RMSprop(list(m_qf1.parameters()), lr=0.5*args.learning_rate)
    ##
    ## END: initializing momentum to 0
    ##


    envs.single_observation_space.dtype = np.float32
    rb = ReplayBuffer(
        args.buffer_size,
        envs.single_observation_space,
        envs.single_action_space,
        device,
        handle_timeout_termination=True,
    )
    start_time = time.time()

    ##
    ## RAN: Need per=sample grads
    ##

    def compute_delta(params, observations, actions, rewards, next_observations, dones):
        observations = observations.unsqueeze(0)
        actions = actions.unsqueeze(0)
        rewards = rewards.unsqueeze(0)
        next_observations = next_observations.unsqueeze(0)
        dones = dones.unsqueeze(0)
        with torch.no_grad():
            next_state_actions = target_actor(next_observations)
        qf1_next_target = functional_call(qf1, (params,), (next_observations, next_state_actions))
        next_q_value = rewards.flatten() + (1 - dones.flatten()) * args.gamma * (qf1_next_target).view(-1)
        qf1_a_values = functional_call(qf1, (params,), (observations, actions)).view(-1)
        delta_ = next_q_value - qf1_a_values
        delta = torch.sum(delta_)
        return delta

    compute_nabla_delta = grad(compute_delta)
    compute_sample_nabla_delta = vmap(compute_nabla_delta, in_dims=(None, 0, 0, 0, 0, 0))

    ##
    ## END
    ##

    # TRY NOT TO MODIFY: start the game
    obs = envs.reset()
    for global_step in range(args.total_timesteps):
        # ALGO LOGIC: put action logic here
        if global_step < args.learning_starts:
            actions = np.array([envs.single_action_space.sample() for _ in range(envs.num_envs)])
        else:
            with torch.no_grad():
                actions = actor(torch.Tensor(obs).to(device))
                actions += torch.normal(0, actor.action_scale * args.exploration_noise)
                actions = actions.cpu().numpy().clip(envs.single_action_space.low, envs.single_action_space.high)

        # TRY NOT TO MODIFY: execute the game and log data.
        next_obs, rewards, dones, infos = envs.step(actions)

        # TRY NOT TO MODIFY: record rewards for plotting purposes
        for info in infos:
            if "episode" in info.keys():
                print(f"global_step={global_step}, episodic_return={info['episode']['r']}")
                writer.add_scalar("charts/episodic_return", info["episode"]["r"], global_step)
                writer.add_scalar("charts/episodic_length", info["episode"]["l"], global_step)
                break

        # TRY NOT TO MODIFY: save data to reply buffer; handle `terminal_observation`
        real_next_obs = next_obs.copy()
        for idx, d in enumerate(dones):
            if d:
                real_next_obs[idx] = infos[idx]["terminal_observation"]
        rb.add(obs, real_next_obs, actions, rewards, dones, infos)

        # TRY NOT TO MODIFY: CRUCIAL step easy to overlook
        obs = next_obs

        # ALGO LOGIC: training.

        if global_step > args.learning_starts:
            data = rb.sample(args.batch_size)
            with torch.no_grad():
                next_state_actions = target_actor(data.next_observations)
            qf1_next_target = qf1(data.next_observations, next_state_actions)
            next_q_value = data.rewards.flatten() + (1 - data.dones.flatten()) * args.gamma * (qf1_next_target).view(-1)

            qf1_a_values = qf1(data.observations, data.actions).view(-1)
            qf1_loss = F.mse_loss(qf1_a_values, next_q_value)

            # optimize the model
            q_optimizer.zero_grad()
            qf1_loss.backward()

            ##
            ## RAN Additions
            ##

            M_optimizer.zero_grad()

            lambda_val = 0.999
            beta = 0.001
            clip_val = 100

            for m, p in zip(m_qf1.parameters(), qf1.parameters()):
                m.data = lambda_val*m.data + beta*p.grad
                # m.data = lambda_val*m.data + beta*torch.clip(p.grad, min=-clip_val, max=clip_val)

            q_optimizer.zero_grad()
            M_optimizer.zero_grad()

            params = {k: v.detach() for k, v in qf1.named_parameters()}
            gs = compute_sample_nabla_delta(params, data.observations, data.actions, data.rewards, data.next_observations, data.dones)

            m_nabla_delta = torch.zeros(args.batch_size).to(device)
            for m, p in zip(m_qf1.named_parameters(), qf1.named_parameters()):
                name = p[0]
                m = m[1]
                p = p[1]
                nabla_deltas = gs[name]
                # nabla_deltas = torch.clip(gs[name], min=-clip_val, max=clip_val)

                m_ = m.unsqueeze(0)

                m_nabla_delta += torch.mul(m_, nabla_deltas).reshape(args.batch_size, -1).sum(1)

            for m, p in zip(m_qf1.named_parameters(), qf1.named_parameters()):
                name = p[0]
                m = m[1]
                p = p[1]
                nabla_deltas = gs[name]
                # nabla_deltas = torch.clip(gs[name], min=-clip_val, max=clip_val)

                if len(p.shape) == 2:
                    m_nabla_delta_ = m_nabla_delta.reshape(-1, 1, 1)
                elif len(p.shape) == 1:
                    m_nabla_delta_ = m_nabla_delta.reshape(-1, 1)

                m_nabla_delta_nabla_delta = torch.mul(m_nabla_delta_, nabla_deltas).mean(0) # Average over the samples of (m^T \nabla \delta) \nabla \delta

                # m.grad = torch.clip(m_nabla_delta_nabla_delta, min=-clip_val, max=clip_val) # let M be updated with its own optimizer
                m.grad = m_nabla_delta_nabla_delta # let M be updated with its own optimizer
                # m.data = m - beta*m_nabla_delta_nabla_delta

            M_optimizer.step() # Does nothing, unless m.grad is set

            for m, p in zip(m_qf1.parameters(), qf1.parameters()):
                p.grad = m.data # let q be updated with its own optimizer
                # p.grad = torch.clip(m.data, min=-clip_val, max=clip_val) # let q be updated with its own optimizer

            ##
            ## END
            ##

            q_optimizer.step()

            if global_step % args.policy_frequency == 0:
                actor_loss = -qf1(data.observations, actor(data.observations)).mean()
                actor_optimizer.zero_grad()
                actor_loss.backward()
                actor_optimizer.step()

                # update the target network
                for param, target_param in zip(actor.parameters(), target_actor.parameters()):
                    target_param.data.copy_(args.tau * param.data + (1 - args.tau) * target_param.data)
                for param, target_param in zip(qf1.parameters(), qf1_target.parameters()):
                    target_param.data.copy_(args.tau * param.data + (1 - args.tau) * target_param.data)

            if global_step % 100 == 0:
                writer.add_scalar("losses/qf1_loss", qf1_loss.item(), global_step)
                writer.add_scalar("losses/actor_loss", actor_loss.item(), global_step)
                writer.add_scalar("losses/qf1_values", qf1_a_values.mean().item(), global_step)
                writer.add_scalar("weights/l1_qf1", l1_params(qf1), global_step)
                writer.add_scalar("weights/l1_m", l1_params(m_qf1), global_step)
                print("SPS:", int(global_step / (time.time() - start_time)))
                writer.add_scalar("charts/SPS", int(global_step / (time.time() - start_time)), global_step)

    envs.close()
    writer.close()
