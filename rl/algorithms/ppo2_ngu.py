"""PPO with Never Give Up style exploration.

https://arxiv.org/abs/1707.06347
https://arxiv.org/abs/2002.06038
"""
from dl.rl import VecEpisodeLogger, RolloutDataManager, NGUVecEnv, ReplayBuffer
from dl.rl import RND, InverseDynamicsEmbedding, NGU, VecActionRewardInObWrapper
from dl.rl import rl_evaluate, rl_record, misc
from dl import logger, Algorithm, Checkpointer, nest
import gin
import os
import time
import torch
import torch.nn as nn
import numpy as np


class PPOActor(object):
    """Actor."""

    def __init__(self, pi, vf):
        """Init."""
        self.pi = pi
        self.vf = vf

    def __call__(self, ob, state_in=None):
        """Produce decision from model."""
        if state_in is None:
            pi_state = None
            vf_state = None
        else:
            pi_state, vf_state = state_in
        outs = self.pi(ob, pi_state)
        outs_vf = self.vf(ob, vf_state)
        data = {'action': outs.action,
                'value': outs_vf.value,
                'logp': outs.dist.log_prob(outs.action),
                'dist': outs.dist.tensorize()}
        if outs.state_out is not None or outs_vf.state_out is not None:
            data['state'] = (outs.state_out, outs_vf.state_out)
        return data


@gin.configurable(blacklist=['logdir'])
class PPO2NGU(Algorithm):
    """PPO with Never Give Up style instrinsic motivation.

    This version of ppo is described in https://arxiv.org/abs/1707.02286 and
    https://github.com/joschu/modular_rl/blob/master/modular_rl/ppo.py
    """

    def __init__(self,
                 logdir,
                 env_fn,
                 policy_fn,
                 value_fn,
                 rnd_net,
                 ide_embedding_net,
                 ide_prediction_net,
                 ide_loss,
                 nenv=1,
                 opt_pi=torch.optim.Adam,
                 opt_vf=torch.optim.Adam,
                 opt_rnd=torch.optim.Adam,
                 opt_ide=torch.optim.Adam,
                 batch_size=32,
                 rollout_length=128,
                 gamma_ext=0.999,
                 gamma_int=0.99,
                 lambda_=0.95,
                 ent_coef=0.01,
                 ngu_coef=0.5,
                 ngu_buffer_capacity=1024,
                 ngu_subsample_freq=32,
                 ngu_updates=4,
                 ngu_batch_size=64,
                 policy_training_starts=500000,
                 buffer_size=100000,
                 norm_advantages=False,
                 epochs_pi=10,
                 epochs_vf=10,
                 max_grad_norm=None,
                 kl_target=0.01,
                 alpha=1.5,
                 eval_num_episodes=1,
                 record_num_episodes=1,
                 gpu=True):
        """Init."""
        self.logdir = logdir
        self.ckptr = Checkpointer(os.path.join(logdir, 'ckpts'))
        self.env_fn = env_fn
        self.nenv = nenv
        self.eval_num_episodes = eval_num_episodes
        self.record_num_episodes = record_num_episodes
        self.ent_coef = ent_coef
        self.ngu_coef = ngu_coef
        self.ngu_updates = ngu_updates
        self.ngu_batch_size = ngu_batch_size
        self.ngu_subsample_freq = ngu_subsample_freq
        self.epochs_pi = epochs_pi
        self.epochs_vf = epochs_vf
        self.max_grad_norm = max_grad_norm
        self.norm_advantages = norm_advantages
        self.kl_target = kl_target
        self.initial_kl_weight = 0.2
        self.kl_weight = self.initial_kl_weight
        self.policy_training_starts = policy_training_starts
        self.alpha = alpha
        self.device = torch.device('cuda:0' if gpu and torch.cuda.is_available()
                                   else 'cpu')
        # self.ngu_coefs = (1 + np.tanh(np.arange(-4, 4, 8/nenv))) / 2. * ngu_coef
        # self.ngu_coefs = torch.from_numpy(self.ngu_coefs).to(self.device)

        self.env = VecEpisodeLogger(env_fn(nenv=nenv))
        self.rnd = RND(rnd_net, opt_rnd, gamma_int,
                       self.env.observation_space.shape, self.device)
        self.ide = InverseDynamicsEmbedding(self.env, ide_embedding_net,
                                            ide_prediction_net, ide_loss,
                                            opt_ide, self.device)
        self.ngu = NGU(self.rnd, self.ide, ngu_buffer_capacity, self.device,
                       gamma=gamma_int)
        self.env = VecActionRewardInObWrapper(NGUVecEnv(self.env, self.ngu),
                                              reward_shape=(2,))

        self.pi = policy_fn(self.env).to(self.device)
        self.vf = value_fn(self.env).to(self.device)
        self.opt_pi = opt_pi(self.pi.parameters())
        self.opt_vf = opt_vf(self.vf.parameters())

        self.gamma = torch.Tensor([gamma_ext, gamma_int]).to(self.device)
        self.data_manager = RolloutDataManager(
            self.env,
            PPOActor(self.pi, self.vf),
            self.device,
            batch_size=batch_size,
            rollout_length=rollout_length,
            gamma=self.gamma,
            lambda_=lambda_,
            norm_advantages=False)
        self.buffer_size = buffer_size
        self.buffer = ReplayBuffer(buffer_size, 1)

        self.mse = nn.MSELoss()

        self.t = 0

    def compute_kl(self):
        """Compute KL divergence of new and old policies."""
        kl = 0
        n = 0
        for batch in self.data_manager.sampler():
            outs = self.pi(batch['obs'])
            old_dist = outs.dist.__class__(**batch['dist'])
            k = old_dist.kl(outs.dist).mean().detach().cpu().numpy()
            s = batch['action'].shape[0]
            kl = (n / (n + s)) * kl + (s / (n + s)) * k
            n += s
        return kl

    def loss_pi(self, batch):
        """Compute loss."""
        outs = self.pi(batch['obs'])
        atarg = batch['atarg'][:, 0] + self.ngu_coef * batch['atarg'][:, 1]
        # atarg /= (1 + self.ngu_coef)
        # atarg = batch['atarg'][:, 1]
        # compute policy loss
        logp = outs.dist.log_prob(batch['action'])
        assert logp.shape == batch['logp'].shape
        ratio = torch.exp(logp - batch['logp'])
        assert ratio.shape == atarg.shape

        old_dist = outs.dist.__class__(**batch['dist'])
        kl = old_dist.kl(outs.dist)
        kl_pen = (kl - 2 * self.kl_target).clamp(min=0).pow(2)
        losses = {}
        losses['pi'] = -(ratio * atarg).mean()
        losses['ent'] = -outs.dist.entropy().mean()
        losses['kl'] = kl.mean()
        losses['kl_pen'] = kl_pen.mean()
        losses['total'] = (losses['pi'] + self.ent_coef * losses['ent']
                           + self.kl_weight * losses['kl'] + 1000 * losses['kl_pen'])
        return losses

    def loss_vf(self, batch):
        v = self.vf(batch['obs']).value
        assert v.shape == batch['vtarg'].shape
        return self.mse(v, batch['vtarg'])

    def step(self):
        """Compute rollout, loss, and update model."""
        self.pi.train()
        self.t += self.data_manager.rollout()
        losses = {'pi': [], 'vf': [], 'ent': [], 'kl': [], 'total': [],
                  'kl_pen': [], 'rnd': [], 'ide': []}
        # if self.norm_advantages:
        #     atarg = self.data_manager.storage.data['atarg']
        #     atarg = atarg[:, 0] + self.ngu_coef * atarg[:, 1]
        #     self.data_manager.storage.data['atarg'][:, 0] -= atarg.mean()
        #     self.data_manager.storage.data['atarg'] /= atarg.std() + 1e-5

        if self.t >= self.policy_training_starts:
            #######################
            # Update pi
            #######################
            kl_too_big = False
            for _ in range(self.epochs_pi):
                if kl_too_big:
                    break
                for batch in self.data_manager.sampler():
                    self.opt_pi.zero_grad()
                    loss = self.loss_pi(batch)
                    # break if new policy is too different from old policy
                    if loss['kl'] > 4 * self.kl_target:
                        kl_too_big = True
                        break
                    loss['total'].backward()

                    for k, v in loss.items():
                        losses[k].append(v.detach().cpu().numpy())

                    if self.max_grad_norm:
                        norm = nn.utils.clip_grad_norm_(self.pi.parameters(),
                                                        self.max_grad_norm)
                        logger.add_scalar('alg/grad_norm', norm, self.t,
                                          time.time())
                        logger.add_scalar('alg/grad_norm_clipped',
                                          min(norm, self.max_grad_norm),
                                          self.t, time.time())
                    self.opt_pi.step()

            #######################
            # Update value function
            #######################
            for _ in range(self.epochs_vf):
                for batch in self.data_manager.sampler():
                    self.opt_vf.zero_grad()
                    loss = self.loss_vf(batch)
                    losses['vf'].append(loss.detach().cpu().numpy())
                    loss.backward()
                    if self.max_grad_norm:
                        norm = nn.utils.clip_grad_norm_(self.vf.parameters(),
                                                        self.max_grad_norm)
                        logger.add_scalar('alg/vf_grad_norm', norm, self.t,
                                          time.time())
                        logger.add_scalar('alg/vf_grad_norm_clipped',
                                          min(norm, self.max_grad_norm),
                                          self.t, time.time())
                    self.opt_vf.step()

        rollout = self.data_manager.storage.data
        lens = self.data_manager.storage.sequence_lengths.int()
        #######################
        # Store rollout_data in replay_buffer
        #######################
        for i in range(self.nenv):
            if self.buffer.num_in_buffer < self.buffer_size or i % self.ngu_subsample_freq == 0:
                for step in range(lens[i]):
                    def _f(x):
                        return x[step][i].cpu().numpy()
                    data = nest.map_structure(_f, rollout)
                    idx = self.buffer.store_observation(data['obs'])
                    self.buffer.store_effect(idx, {
                        'done': data['done'],
                        'action': data['action'],
                        'reward': data['reward']
                    })

        #######################
        # Update NGU
        #######################
        for _ in range(self.ngu_updates):
            batch = self.buffer.sample(self.ngu_batch_size)

            def _to_torch(data):
                if isinstance(data, np.ndarray):
                    return torch.from_numpy(data).to(self.device)
                else:
                    return data
            batch = nest.map_structure(_to_torch, batch)
            loss = self.ngu.update_rnd(batch['obs']['ob'])
            losses['rnd'].append(loss.detach().cpu().numpy())
            not_done = torch.logical_not(batch['done'])
            loss = self.ngu.update_ide(batch['obs']['ob'][not_done],
                                       batch['next_obs']['ob'][not_done],
                                       batch['action'][not_done].long())
            losses['ide'].append(loss.detach().cpu().numpy())

        for k, v in losses.items():
            if len(v) == 0:
                continue
            logger.add_scalar(f'loss/{k}', np.mean(v), self.t, time.time())

        # update weight on kl to match kl_target.
        if self.t >= self.policy_training_starts:
            kl = self.compute_kl()
            if kl > 10.0 * self.kl_target and self.kl_weight < self.initial_kl_weight:
                self.kl_weight = self.initial_kl_weight
            elif kl > 1.3 * self.kl_target:
                self.kl_weight *= self.alpha
            elif kl < 0.7 * self.kl_target:
                self.kl_weight /= self.alpha
        else:
            kl = 0.0

        logger.add_scalar('alg/kl', kl, self.t, time.time())
        logger.add_scalar('alg/kl_weight', self.kl_weight, self.t, time.time())
        avg_return = self.data_manager.storage.data['return'][:, 0].mean(dim=0)
        avg_return = (avg_return[0] + self.ngu_coef * avg_return[1]) / (1 + self.ngu_coef)
        logger.add_scalar('alg/return', avg_return, self.t, time.time())

        # log value errors
        errors = []
        for batch in self.data_manager.sampler():
            errors.append(batch['vpred'] - batch['q_mc'])
        errors = torch.cat(errors)
        logger.add_scalar('alg/value_error_mean',
                          errors.mean().cpu().numpy(), self.t, time.time())
        logger.add_scalar('alg/value_error_std',
                          errors.std().cpu().numpy(), self.t, time.time())

        return self.t

    def evaluate(self):
        """Evaluate model."""
        self.pi.eval()
        misc.set_env_to_eval_mode(self.env)

        # Eval policy
        os.makedirs(os.path.join(self.logdir, 'eval'), exist_ok=True)
        outfile = os.path.join(self.logdir, 'eval',
                               self.ckptr.format.format(self.t) + '.json')
        stats = rl_evaluate(self.env, self.pi, self.eval_num_episodes,
                            outfile, self.device)
        logger.add_scalar('eval/mean_episode_reward', stats['mean_reward'],
                          self.t, time.time())
        logger.add_scalar('eval/mean_episode_length', stats['mean_length'],
                          self.t, time.time())

        # Record policy
        os.makedirs(os.path.join(self.logdir, 'video'), exist_ok=True)
        outfile = os.path.join(self.logdir, 'video',
                               self.ckptr.format.format(self.t) + '.mp4')
        rl_record(self.env, self.pi, self.record_num_episodes, outfile,
                  self.device)

        self.pi.train()
        misc.set_env_to_train_mode(self.env)

    def save(self):
        """State dict."""
        state_dict = {
            'pi': self.pi.state_dict(),
            'vf': self.vf.state_dict(),
            'opt_pi': self.opt_pi.state_dict(),
            'opt_vf': self.opt_vf.state_dict(),
            'kl_weight': self.kl_weight,
            'env': misc.env_state_dict(self.env),
            'ngu': self.ngu.state_dict(),
            't': self.t
        }
        buffer_dict = self.buffer.state_dict()
        state_dict['buffer_format'] = nest.get_structure(buffer_dict)
        self.ckptr.save(state_dict, self.t)

        # save buffer seperately and only once (because it can be huge)
        np.savez(os.path.join(self.ckptr.ckptdir, 'buffer.npz'),
                 **{f'{i:04d}': x for i, x in
                    enumerate(nest.flatten(buffer_dict))})

    def load(self, t=None):
        """Load state dict."""
        state_dict = self.ckptr.load(t)
        if state_dict is None:
            self.t = 0
            return self.t
        self.pi.load_state_dict(state_dict['pi'])
        self.vf.load_state_dict(state_dict['vf'])
        self.opt_pi.load_state_dict(state_dict['opt_pi'])
        self.opt_vf.load_state_dict(state_dict['opt_vf'])
        self.kl_weight = state_dict['kl_weight']
        misc.env_load_state_dict(self.env, state_dict['env'])
        self.ngu.load_state_dict(state_dict['ngu'])
        self.t = state_dict['t']

        buffer_format = state_dict['buffer_format']
        buffer_state = dict(np.load(os.path.join(self.ckptr.ckptdir,
                                                 'buffer.npz'),
                                    allow_pickle=True))
        buffer_state = nest.flatten(buffer_state)
        self.buffer.load_state_dict(nest.pack_sequence_as(buffer_state,
                                                          buffer_format))
        self.buffer.env_reset()
        return self.t

    def close(self):
        """Close environment."""
        try:
            self.env.close()
        except Exception:
            pass


if __name__ == '__main__':

    import unittest
    import shutil
    from dl import train
    from dl.rl.envs import make_atari_env
    from dl.rl.modules import Policy, ValueFunction
    from dl.rl.modules import PolicyBase, ValueFunctionBase
    from dl.rl.util import conv_out_shape
    from dl.modules import Categorical
    import torch.nn.functional as F
    from functools import partial

    class RNDNet(nn.Module):
        """Deep network from https://www.nature.com/articles/nature14236."""

        def __init__(self, shape):
            """Build network."""
            super().__init__()
            self.conv1 = nn.Conv2d(4, 32, 8, 4)
            self.conv2 = nn.Conv2d(32, 64, 4, 2)
            self.conv3 = nn.Conv2d(64, 64, 3, 1)
            shape = shape[1:]
            for c in [self.conv1, self.conv2, self.conv3]:
                shape = conv_out_shape(shape, c)
            self.nunits = 64 * np.prod(shape)
            self.fc = nn.Linear(self.nunits, 256)

        def forward(self, x):
            """Forward."""
            x = F.relu(self.conv1(x))
            x = F.relu(self.conv2(x))
            x = F.relu(self.conv3(x))
            return self.fc(x.view(-1, self.nunits))

    class IDENet(nn.Module):
        """Deep network from https://www.nature.com/articles/nature14236."""

        def __init__(self, observation_space):
            """Build network."""
            super().__init__()
            shape = observation_space.shape
            self.conv1 = nn.Conv2d(4, 32, 8, 4)
            self.conv2 = nn.Conv2d(32, 64, 4, 2)
            self.conv3 = nn.Conv2d(64, 64, 3, 1)
            shape = shape[1:]
            for c in [self.conv1, self.conv2, self.conv3]:
                shape = conv_out_shape(shape, c)
            self.nunits = 64 * np.prod(shape)
            self.fc = nn.Linear(self.nunits, 256)

        def forward(self, x):
            """Forward."""
            x = x.float() / 255.
            x = F.relu(self.conv1(x))
            x = F.relu(self.conv2(x))
            x = F.relu(self.conv3(x))
            return self.fc(x.view(-1, self.nunits))

    class NatureDQN(PolicyBase):
        """Deep network from https://www.nature.com/articles/nature14236."""

        def build(self):
            """Build network."""
            self.conv1 = nn.Conv2d(4, 32, 8, 4)
            self.conv2 = nn.Conv2d(32, 64, 4, 2)
            self.conv3 = nn.Conv2d(64, 64, 3, 1)
            shape = self.observation_space.shape[1:]
            for c in [self.conv1, self.conv2, self.conv3]:
                shape = conv_out_shape(shape, c)
            self.nunits = 64 * np.prod(shape)
            self.fc = nn.Linear(self.nunits, 512)
            self.dist = Categorical(512, self.action_space.n)

        def forward(self, x):
            """Forward."""
            x = x['ob'].float() / 255.
            x = F.relu(self.conv1(x))
            x = F.relu(self.conv2(x))
            x = F.relu(self.conv3(x))
            x = F.relu(self.fc(x.view(-1, self.nunits)))
            return self.dist(x)

    class NatureDQNVF(ValueFunctionBase):
        """Deep network from https://www.nature.com/articles/nature14236."""

        def build(self):
            """Build network."""
            self.conv1 = nn.Conv2d(4, 32, 8, 4)
            self.conv2 = nn.Conv2d(32, 64, 4, 2)
            self.conv3 = nn.Conv2d(64, 64, 3, 1)
            shape = self.observation_space.shape[1:]
            for c in [self.conv1, self.conv2, self.conv3]:
                shape = conv_out_shape(shape, c)
            self.nunits = 64 * np.prod(shape)
            self.fc = nn.Linear(self.nunits, 512)
            self.vf = nn.Linear(512, 2)

        def forward(self, x):
            """Forward."""
            x = x['ob'].float() / 255.
            x = F.relu(self.conv1(x))
            x = F.relu(self.conv2(x))
            x = F.relu(self.conv3(x))
            x = F.relu(self.fc(x.view(-1, self.nunits)))
            return self.vf(x)

    class PredictionNet(nn.Module):
        def __init__(self, action_space):
            super().__init__()
            self.fc1 = nn.Linear(512, 32)
            self.dist = Categorical(32, action_space.n)

        def forward(self, x1, x2):
            x = torch.cat((x1, x2), dim=1)
            x = F.relu(self.fc1(x))
            return self.dist(x)

    class Loss(nn.Module):
        def forward(self, dist, actions):
            loss = F.cross_entropy(dist.logits, actions)
            return loss

    class TestPPO2RND(unittest.TestCase):
        """Test case."""

        def test_feed_forward_ppo2(self):
            """Test feed forward ppo2."""
            def env_fn(nenv):
                return make_atari_env('Pong', nenv, frame_stack=4)

            def policy_fn(env):
                return Policy(NatureDQN(env.observation_space,
                                        env.action_space))

            def vf_fn(env):
                return ValueFunction(NatureDQNVF(env.observation_space,
                                                 env.action_space))

            ppo = partial(PPO2NGU, env_fn=env_fn, policy_fn=policy_fn,
                          value_fn=vf_fn, rnd_net=RNDNet, ide_embedding_net=IDENet,
                          ide_prediction_net=PredictionNet, ide_loss=Loss)
            train('test', ppo, maxt=1000, eval=True, eval_period=1000)
            shutil.rmtree('test')

    unittest.main()
