import numpy as np
import torch
import torch.nn as nn
from torch.optim import Adam
import gym
import time
import scipy.signal
# import spinup.algos.pytorch.ppo.core as core
# from core_1 import Actor, Critic
from core_lstm import userCritic, userActor
from env import create_train_env
from spinup.utils.logx import EpochLogger
from spinup.utils.mpi_pytorch import setup_pytorch_for_mpi, sync_params, mpi_avg_grads
from spinup.utils.mpi_tools import mpi_fork, mpi_avg, proc_id, mpi_statistics_scalar, num_procs

device = torch.device('cuda')

def combined_shape(length, shape=None):
    if shape is None:
        return (length,)
    return (length, shape) if np.isscalar(shape) else (length, *shape)

def discount_cumsum(x, discount):
    """
    magic from rllab for computing discounted cumulative sums of vectors.

    input:
        vector x,
        [x0,
         x1,
         x2]

    output:
        [x0 + discount * x1 + discount^2 * x2,
         x1 + discount * x2,
         x2]
    """
    return scipy.signal.lfilter([1], [1, float(-discount)], x[::-1], axis=0)[::-1]

class PPOBuffer:
    """
    A buffer for storing trajectories experienced by a PPO agent interacting
    with the environment, and using Generalized Advantage Estimation (GAE-Lambda)
    for calculating the advantages of state-action pairs.
    """

    def __init__(self, obs_dim, act_dim, hidden_dim, size, gamma=0.99, lam=0.95):
        self.obs_buf = np.zeros(combined_shape(size, obs_dim), dtype=np.float32)
        self.act_buf = np.zeros(combined_shape(size, act_dim), dtype=np.float32)
        self.adv_buf = np.zeros(size, dtype=np.float32)
        self.rew_buf = np.zeros(size, dtype=np.float32)
        self.ret_buf = np.zeros(size, dtype=np.float32)
        self.val_buf = np.zeros(size, dtype=np.float32)
        self.logp_buf = np.zeros(size, dtype=np.float32)
        self.hidden_h_buf = np.zeros(combined_shape(size, hidden_dim), dtype=np.float32)
        self.hidden_c_buf = np.zeros(combined_shape(size, hidden_dim), dtype=np.float32)
        print(self.hidden_h_buf.shape)
        self.gamma, self.lam = gamma, lam
        self.ptr, self.path_start_idx, self.max_size = 0, 0, size

    def store(self, obs, act, rew, val, logp, hidden_h, hidden_c):
        """
        Append one timestep of agent-environment interaction to the buffer.
        """
        assert self.ptr < self.max_size     # buffer has to have room so you can store
        self.obs_buf[self.ptr] = obs
        self.act_buf[self.ptr] = act
        self.rew_buf[self.ptr] = rew
        self.val_buf[self.ptr] = val
        self.logp_buf[self.ptr] = logp
        self.hidden_h_buf[self.ptr] = hidden_h
        self.hidden_c_buf[self.ptr] = hidden_c
        self.ptr += 1

    def finish_path(self, last_val=0):
        """
        Call this at the end of a trajectory, or when one gets cut off
        by an epoch ending. This looks back in the buffer to where the
        trajectory started, and uses rewards and value estimates from
        the whole trajectory to compute advantage estimates with GAE-Lambda,
        as well as compute the rewards-to-go for each state, to use as
        the targets for the value function.

        The "last_val" argument should be 0 if the trajectory ended
        because the agent reached a terminal state (died), and otherwise
        should be V(s_T), the value function estimated for the last state.
        This allows us to bootstrap the reward-to-go calculation to account
        for timesteps beyond the arbitrary episode horizon (or epoch cutoff).
        """

        path_slice = slice(self.path_start_idx, self.ptr)
        rews = np.append(self.rew_buf[path_slice], last_val)
        vals = np.append(self.val_buf[path_slice], last_val)
        
        # the next two lines implement GAE-Lambda advantage calculation
        deltas = rews[:-1] + self.gamma * vals[1:] - vals[:-1]
        self.adv_buf[path_slice] = discount_cumsum(deltas, self.gamma * self.lam)
        
        # the next line computes rewards-to-go, to be targets for the value function
        self.ret_buf[path_slice] = discount_cumsum(rews, self.gamma)[:-1]
        
        self.path_start_idx = self.ptr

    def get(self):
        """
        Call this at the end of an epoch to get all of the data from
        the buffer, with advantages appropriately normalized (shifted to have
        mean zero and std one). Also, resets some pointers in the buffer.
        """
        assert self.ptr == self.max_size    # buffer has to be full before you can get
        self.ptr, self.path_start_idx = 0, 0
        # the next two lines implement the advantage normalization trick
        adv_mean, adv_std = mpi_statistics_scalar(self.adv_buf)
        self.adv_buf = (self.adv_buf - adv_mean) / adv_std
        data = dict(obs=self.obs_buf, act=self.act_buf, ret=self.ret_buf,
                    adv=self.adv_buf, logp=self.logp_buf, hidden_h=self.hidden_h_buf, hidden_c=self.hidden_c_buf)
        #data.to(device)
        return {k: torch.as_tensor(v, dtype=torch.float32).to(device) for k,v in data.items()}



def ppo(env_fn, actor=nn.Module, critic=nn.Module, ac_kwargs=dict(), seed=0,
        steps_per_epoch=4000, epochs=50, gamma=0.99, clip_ratio=0.2, pi_lr=3e-4,
        vf_lr=1e-3, train_pi_iters=80, train_v_iters=80, lam=0.97, max_ep_len=1000,
        target_kl=0.01, logger_kwargs=dict(), save_freq=10, pretrain=None):
    """
    Proximal Policy Optimization (by clipping), 

    with early stopping based on approximate KL

    Args:
        env_fn : A function which creates a copy of the environment.
            The environment must satisfy the OpenAI Gym API.

        actor_critic: The constructor method for a PyTorch Module with a 
            ``step`` method, an ``act`` method, a ``pi`` module, and a ``v`` 
            module. The ``step`` method should accept a batch of observations 
            and return:

            ===========  ================  ======================================
            Symbol       Shape             Description
            ===========  ================  ======================================
            ``a``        (batch, act_dim)  | Numpy array of actions for each 
                                           | observation.
            ``v``        (batch,)          | Numpy array of value estimates
                                           | for the provided observations.
            ``logp_a``   (batch,)          | Numpy array of log probs for the
                                           | actions in ``a``.
            ===========  ================  ======================================

            The ``act`` method behaves the same as ``step`` but only returns ``a``.

            The ``pi`` module's forward call should accept a batch of 
            observations and optionally a batch of actions, and return:

            ===========  ================  ======================================
            Symbol       Shape             Description
            ===========  ================  ======================================
            ``pi``       N/A               | Torch Distribution object, containing
                                           | a batch of distributions describing
                                           | the policy for the provided observations.
            ``logp_a``   (batch,)          | Optional (only returned if batch of
                                           | actions is given). Tensor containing 
                                           | the log probability, according to 
                                           | the policy, of the provided actions.
                                           | If actions not given, will contain
                                           | ``None``.
            ===========  ================  ======================================

            The ``v`` module's forward call should accept a batch of observations
            and return:

            ===========  ================  ======================================
            Symbol       Shape             Description
            ===========  ================  ======================================
            ``v``        (batch,)          | Tensor containing the value estimates
                                           | for the provided observations. (Critical: 
                                           | make sure to flatten this!)
            ===========  ================  ======================================


        ac_kwargs (dict): Any kwargs appropriate for the ActorCritic object 
            you provided to PPO.

        seed (int): Seed for random number generators.

        steps_per_epoch (int): Number of steps of interaction (state-action pairs) 
            for the agent and the environment in each epoch.

        epochs (int): Number of epochs of interaction (equivalent to
            number of policy updates) to perform.

        gamma (float): Discount factor. (Always between 0 and 1.)

        clip_ratio (float): Hyperparameter for clipping in the policy objective.
            Roughly: how far can the new policy go from the old policy while 
            still profiting (improving the objective function)? The new policy 
            can still go farther than the clip_ratio says, but it doesn't help
            on the objective anymore. (Usually small, 0.1 to 0.3.) Typically
            denoted by :math:`\epsilon`. 

        pi_lr (float): Learning rate for policy optimizer.

        vf_lr (float): Learning rate for value function optimizer.

        train_pi_iters (int): Maximum number of gradient descent steps to take 
            on policy loss per epoch. (Early stopping may cause optimizer
            to take fewer than this.)

        train_v_iters (int): Number of gradient descent steps to take on 
            value function per epoch.

        lam (float): Lambda for GAE-Lambda. (Always between 0 and 1,
            close to 1.)

        max_ep_len (int): Maximum length of trajectory / episode / rollout.

        target_kl (float): Roughly what KL divergence we think is appropriate
            between new and old policies after an update. This will get used 
            for early stopping. (Usually small, 0.01 or 0.05.)

        logger_kwargs (dict): Keyword args for EpochLogger.

        save_freq (int): How often (in terms of gap between epochs) to save
            the current policy and value function.

    """

    # Special function to avoid certain slowdowns from PyTorch + MPI combo.
    setup_pytorch_for_mpi()

    # Set up logger and save configuration
    logger = EpochLogger(**logger_kwargs)
    logger.save_config(locals())

    # Random seed
    seed += 10000 * proc_id()
    torch.manual_seed(seed)
    np.random.seed(seed)

    # Instantiate environment
    env = env_fn()
    # obs_dim = env.observation_space.shape
    # act_dim = env.action_space.shape
    obs_dim = env.observation_space.shape
    act_dim = env.action_space.n
    # Create actor-critic module
    # ac_pi = actor(np.prod(obs_dim), act_dim, hidden_sizes=[64,64], activation=nn.Tanh)#env.observation_space, env.action_space, nn.ReLU)
    # ac_v = critic(np.prod(obs_dim), hidden_sizes=[64,64], activation=nn.Tanh)#env.observation_space, nn.ReLU)
    if pretrain != None:
        ac_pi = torch.load(pretrain)
    else:
        ac_pi = actor(obs_dim[0], act_dim, hidden_sizes=[64, 64], activation=nn.Tanh, pretrain=pretrain)  # env.observation_space, env.action_space, nn.ReLU)
    ac_v = critic(obs_dim[0], hidden_sizes=[64, 64], activation=nn.Tanh)  # env.observation_space, nn.ReLU)

    device = torch.device('cuda') if torch.cuda.is_available() else torch.device('cup')
    ac_pi.to(device)
    ac_v.to(device)
    #ac_pi = nn.DataParallel(ac_pi)
    #ac_v = nn.DataParallel(ac_v)

    # Sync params across processes
   # sync_params(ac_pi)
   # sync_params(ac_v)

    # Count variables
    def count_vars(module):
        return sum([np.prod(p.shape) for p in module.parameters()])
    var_counts = tuple(count_vars(module) for module in [ac_pi, ac_v])
    logger.log('\nNumber of parameters: \t pi: %d, \t v: %d\n'%var_counts)

    # Set up experience buffer
    local_steps_per_epoch = int(steps_per_epoch / num_procs())
    buf = PPOBuffer(obs_dim, env.action_space.shape, 512, local_steps_per_epoch, gamma, lam)
          # PPOBuffer(obs_dim, env.action_space.shape, local_steps_per_epoch, gamma, lam)
    # Set up function for computing PPO policy loss
    def compute_loss_pi(data):
        obs, act, adv, logp_old , hidden_h, hidden_c = data['obs'], data['act'], data['adv'], data['logp'], data['hidden_h'], data['hidden_c']

        # Policy loss
        pi, logp ,_ = ac_pi(obs, act, (hidden_h, hidden_c))
        ratio = torch.exp(logp - logp_old)
        clip_adv = torch.clamp(ratio, 1-clip_ratio, 1+clip_ratio) * adv
        loss_pi = -(torch.min(ratio * adv, clip_adv)).mean()

        # Useful extra info
        approx_kl = (logp_old - logp).mean().item()
        ent = pi.entropy().mean().item()
        clipped = ratio.gt(1+clip_ratio) | ratio.lt(1-clip_ratio)
        clipfrac = torch.as_tensor(clipped, dtype=torch.float32).mean().item()
        pi_info = dict(kl=approx_kl, ent=ent, cf=clipfrac)

        return loss_pi, pi_info

    # Set up function for computing value loss
    def compute_loss_v(data):
        obs, ret ,hidden_h, hidden_c = data['obs'], data['ret'], data['hidden_h'], data['hidden_c']
        return ((ac_v(obs, (hidden_h, hidden_c)) - ret)**2).mean()

    # Set up optimizers for policy and value function
    pi_optimizer = Adam(ac_pi.parameters(), lr=pi_lr)
    vf_optimizer = Adam(ac_v.parameters(), lr=vf_lr)

    # Set up model saving
    logger.setup_pytorch_saver(ac_pi)

    def update():
        data = buf.get()

        pi_l_old, pi_info_old = compute_loss_pi(data)
        pi_l_old = pi_l_old.item()
        v_l_old = compute_loss_v(data).item()

        # Train policy with multiple steps of gradient descent
        for i in range(train_pi_iters):
            pi_optimizer.zero_grad()
            loss_pi, pi_info = compute_loss_pi(data)
            kl = mpi_avg(pi_info['kl'])
            if kl > 1.5 * target_kl:
                logger.log('Early stopping at step %d due to reaching max kl.'%i)
                break
            loss_pi.backward()
            mpi_avg_grads(ac_pi)    # average grads across MPI processes
            pi_optimizer.step()

        logger.store(StopIter=i)

        # Value function learning
        for i in range(train_v_iters):
            vf_optimizer.zero_grad()
            loss_v = compute_loss_v(data)
            loss_v.backward()
            mpi_avg_grads(ac_v)    # average grads across MPI processes
            vf_optimizer.step()

        # Log changes from update
        kl, ent, cf = pi_info['kl'], pi_info_old['ent'], pi_info['cf']
        logger.store(LossPi=pi_l_old, LossV=v_l_old,
                     KL=kl, Entropy=ent, ClipFrac=cf,
                     DeltaLossPi=(loss_pi.item() - pi_l_old),
                     DeltaLossV=(loss_v.item() - v_l_old))

    # Prepare for interaction with environment
    start_time = time.time()
    o, ep_ret, ep_len = env.reset(), 0, 0

    # Main loop: collect experience in env and update/log each epoch
    for epoch in range(epochs):
        hidden = (torch.zeros((1, 512), dtype=torch.float).to(device), torch.zeros((1, 512), dtype=torch.float).to(device))
        for t in range(local_steps_per_epoch):
            # a, v, logp = ac.step(torch.as_tensor(o, dtype=torch.float32))
            with torch.no_grad():
                rr = torch.from_numpy(o.copy()).float().to(device)#.unsqueeze(0)
                pi, _, hidden_ = ac_pi(rr, None, hidden)
                a = pi.sample()
                # logp_a = self.pi._log_prob_from_distribution(pi, a)
                logp = pi.log_prob(a)#.sum(axis=-1)
                v = ac_v(torch.as_tensor(o, dtype=torch.float32).to(device), hidden)

            next_o, r, d, _ = env.step(a.cpu().numpy().item())
            ep_ret += r
            ep_len += 1
            #hidden = hidden_ 

            # save and log
            #print(hidden[0].shape)
            buf.store(o, a.cpu().numpy(), r, v.cpu().numpy(), logp.cpu().numpy(), hidden[0].cpu().numpy(), hidden[1].cpu().numpy())
            logger.store(VVals=v.cpu().numpy())
            
            # Update obs (critical!)
            o = next_o
            hidden = hidden_

            timeout = ep_len == max_ep_len
            terminal = d #or timeout
            epoch_ended = t==local_steps_per_epoch-1

            if terminal or epoch_ended:
                if epoch_ended and not(terminal):
                    print('Warning: trajectory cut off by epoch at %d steps.'%ep_len, flush=True)
                # if trajectory didn't reach terminal state, bootstrap value target
                if epoch_ended:
                    print('epoch_end')
                    # _, v, _ = ac.step(torch.as_tensor(o, dtype=torch.float32))
                    with torch.no_grad():
                        v =ac_v(torch.from_numpy(o).float().to(device), hidden).cpu().numpy()
                else:
                    print('epret :',ep_ret)
                    v = 0
                    hidden= (torch.zeros((1, 512), dtype=torch.float).to(device), torch.zeros((1, 512), dtype=torch.float).to(device))
                buf.finish_path(v)
                if terminal:
                    # only save EpRet / EpLen if trajectory finished
                    logger.store(EpRet=ep_ret, EpLen=ep_len)
                o, ep_ret, ep_len = env.reset(), 0, 0


        # Save model
        if (epoch % save_freq == 0) or (epoch == epochs-1):
            logger.save_state({'env': env}, None)

        # Perform PPO update!
        update()

        # Log info about epoch
        logger.log_tabular('Epoch', epoch)
        logger.log_tabular('EpRet', with_min_and_max=True)
        logger.log_tabular('EpLen', average_only=True)
        logger.log_tabular('VVals', with_min_and_max=True)
        logger.log_tabular('TotalEnvInteracts', (epoch+1)*steps_per_epoch)
        logger.log_tabular('LossPi', average_only=True)
        logger.log_tabular('LossV', average_only=True)
        logger.log_tabular('DeltaLossPi', average_only=True)
        logger.log_tabular('DeltaLossV', average_only=True)
        logger.log_tabular('Entropy', average_only=True)
        logger.log_tabular('KL', average_only=True)
        logger.log_tabular('ClipFrac', average_only=True)
        logger.log_tabular('StopIter', average_only=True)
        logger.log_tabular('Time', time.time()-start_time)
        logger.dump_tabular()

if __name__ == '__main__':
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument('--env', type=str, default='LunarLander-v2')#'HalfCheetah-v2')
    parser.add_argument('--hid', type=int, default=64)
    parser.add_argument('--l', type=int, default=2)
    parser.add_argument('--gamma', type=float, default=0.999)
    parser.add_argument('--seed', '-s', type=int, default=0)
    parser.add_argument('--cpu', type=int, default=1)
    parser.add_argument('--steps', type=int, default=4000)
    parser.add_argument('--epochs', type=int, default=350)
    parser.add_argument('--pretrain', type=str, default='/root/lele/spinningup/spinningup/data/ppo_0715/ppo_0715_s0/pyt_save/model.pt')
    parser.add_argument('--exp_name', type=str, default='ppo_lstm_1106')
    args = parser.parse_args()

    import os
    os.environ['KMP_DUPLICATE_LIB_OK'] = 'True'
    mpi_fork(args.cpu)  # run parallel code with mpi

    from spinup.utils.run_utils import setup_logger_kwargs
    logger_kwargs = setup_logger_kwargs(args.exp_name, args.seed)
    # from baselines.common.vec_env.subproc_vec_env import SubprocVecEnv
    env_fn = lambda : create_train_env(1,1,'complex')
    # env_fn = SubprocVecEnv([])
    # env_fn = lambda : JoypadSpace(gym_super_mario_bros.make("SuperMarioBros-{}-{}-v0".format(1, 1)), gym_super_mario_bros.actions.COMPLEX_MOVEMENT)
    ppo(env_fn, actor=userActor, critic=userCritic,#core.MLPActorCritic, #gym.make(args.env)
        ac_kwargs=dict(hidden_sizes=[args.hid]*args.l), gamma=args.gamma, 
        seed=args.seed, steps_per_epoch=args.steps, epochs=args.epochs,
        logger_kwargs=logger_kwargs, clip_ratio=0.2, pi_lr=0.0003, vf_lr=0.0001, pretrain=None)#args.pretrain)

