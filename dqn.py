import argparse
import copy
import glob
import math
import re
from collections import deque
from collections import namedtuple
from random import random, sample

import cv2
import gym
import gym_ple
import numpy as np
import pylab
import torch
from torch import nn, optim
from torch.autograd import Variable
from torch.nn import functional as F
from torchvision import transforms as T

GAME_NAME = 'FlappyBird-v0'  # only Pygames are supported

# Training
BATCH_SIZE = 16

# Epsilon
EPSILON_START = 1.0
EPSILON_END = 0.05
EPSILON_DECAY = 20000

# ETC Options
TARGET_UPDATE_INTERVAL = 500
CHECKPOINT_INTERVAL = 5000

parser = argparse.ArgumentParser(description='DQN Configuration')
parser.add_argument('--step', default=None, type=int)
parser.add_argument('--load_latest', default=True, type=bool)
parser.add_argument('--checkpoint', default=None, type=str)
parser.add_argument('--mode', default='play', type=str, help='[play, train]')
parser.add_argument('--game', default='FlappyBird-v0', type=str, help='only Pygames are supported')


class ReplayMemory(object):
    def __init__(self, capacity=20000):
        self.capacity = capacity
        self.memory = deque(maxlen=self.capacity)
        self.Transition = namedtuple('Transition', ('state', 'action', 'reward', 'next_state'))
        self._available = False

    def put(self, state: np.array, action: torch.LongTensor, reward: np.array, next_state: np.array):
        """
        저장시 모두 Torch Tensor로 변경해준다음에 저장을 합니다.
        action은 select_action()함수에서부터 LongTensor로 리턴해주기 때문에,
        여기서 변경해줄필요는 없음
        """
        state = torch.FloatTensor(state)
        reward = torch.FloatTensor([reward])
        next_state = torch.FloatTensor(next_state)
        transition = self.Transition(state=state, action=action, reward=reward, next_state=next_state)
        self.memory.append(transition)

    def sample(self, batch_size):
        transitions = sample(self.memory, batch_size)
        return self.Transition(*(zip(*transitions)))

    def size(self):
        return len(self.memory)

    def is_available(self):
        if self._available:
            return True

        if len(self.memory) > BATCH_SIZE:
            self._available = True
        return self._available


class DQN(nn.Module):
    def __init__(self, n_action):
        super(DQN, self).__init__()
        self.n_action = n_action

        self.conv1 = nn.Conv3d(3, 16, kernel_size=5, stride=1, padding=1)  # (In Channel, Out Channel, ...)
        self.conv2 = nn.Conv3d(16, 32, kernel_size=4, stride=1, padding=1)
        self.conv3 = nn.Conv3d(32, 32, kernel_size=3, stride=1, padding=1)

        self.bn1 = nn.BatchNorm3d(16)
        self.bn2 = nn.BatchNorm3d(32)
        self.bn3 = nn.BatchNorm3d(32)

        self.affine1 = nn.Linear(209952, 256)
        self.affine2 = nn.Linear(256, self.n_action)

    def forward(self, x):
        h = F.leaky_relu(self.bn1(self.conv1(x)))
        h = F.leaky_relu(self.bn2(self.conv2(h)))
        h = F.leaky_relu(self.bn3(self.conv3(h)))
        h = self.affine1(h.view(h.size(0), -1))
        h = self.affine2(h)
        return h


class Environment(object):
    def __init__(self, game, width=84, height=84):
        self.game = gym.make(game)
        self.width = width
        self.height = height
        self._toTensor = T.Compose([T.ToPILImage(), T.ToTensor()])
        gym_ple

    def play_sample(self, mode: str = 'human'):
        observation = self.game.reset()

        while True:
            screen = self.game.render(mode=mode)
            if mode == 'rgb_array':
                screen = self.preprocess(screen)
            action = self.game.action_space.sample()
            observation, reward, done, info = self.game.step(action)
            if done:
                break
        self.game.close()

    def preprocess(self, screen):
        preprocessed: np.array = cv2.resize(screen, (self.height, self.width))  # 84 * 84 로 변경
        preprocessed: np.array = preprocessed.transpose((2, 0, 1))  # (C, W, H) 로 변경
        preprocessed: np.array = preprocessed.astype('float32') / 255.
        return preprocessed

    def init(self):
        """
        @return observation
        """
        return self.game.reset()

    def get_screen(self):
        screen = self.game.render('rgb_array')
        screen = self.preprocess(screen)
        return screen

    def step(self, action: int):
        observation, reward, done, info = self.game.step(action)
        return observation, reward, done, info

    def reset(self):
        """
        :return: observation array
        """
        observation = self.game.reset()
        observation = self.preprocess(observation)
        return observation

    @property
    def action_space(self):
        return self.game.action_space.n


class Agent(object):
    def __init__(self, game: str, cuda=True, action_repeat: int = 4, frame_skipping=4):
        # Init
        self.action_repeat: int = action_repeat
        self.frame_skipping: int = frame_skipping
        self._state_buffer = deque(maxlen=self.action_repeat)
        self.step = 0

        self._play_steps = deque(maxlen=5)

        # Environment
        self.env = Environment(game)

        # DQN Model
        self.dqn: DQN = DQN(self.env.action_space)
        if cuda:
            self.dqn.cuda()

        # DQN Target Model
        self.target: DQN = copy.deepcopy(self.dqn)

        # Optimizer
        self.optimizer = optim.RMSprop(self.dqn.parameters(), lr=0.007)

        # Replay Memory
        self.replay = ReplayMemory()

        # Epsilon
        self.epsilon = EPSILON_START

    def select_action(self, states: np.array) -> torch.LongTensor:
        """
        :param states: 게임화면
        :return: LongTensor (int64) 값이며, [[index]] 이런 형태를 갖고 있다.
        추후 gather와 함께 쓰기 위해서 index값이 필요하다
        """
        # Decrease epsilon value
        self.epsilon = EPSILON_END + (EPSILON_START - EPSILON_END) * \
                                     math.exp(-1. * self.step / EPSILON_DECAY) + 5 / (1 + self.play_step)

        if self.epsilon > random():
            # Random Action
            action = torch.LongTensor([[self.env.game.action_space.sample()]])
            return action

        # max(dimension) 이 들어가며 tuple을 return값으로 내놓는다.
        # tuple안에는 (FloatTensor, LongTensor)가 있으며
        # FloatTensor는 가장 큰 값
        # LongTensor에는 가장 큰 값의 index가 있다.
        states = states.reshape(1, 3, self.action_repeat, self.env.width, self.env.height)
        states_variable: Variable = Variable(torch.FloatTensor(states).cuda(), volatile=True)
        action = self.dqn(states_variable).data.cpu().max(1)[1]
        return action

    def get_initial_states(self):
        state = self.env.reset()
        state = self.env.get_screen()
        states = np.stack([state for _ in range(self.action_repeat)], axis=0)

        self._state_buffer = deque(maxlen=self.action_repeat)
        for _ in range(self.action_repeat):
            self._state_buffer.append(state)
        return states

    def add_state(self, state):
        self._state_buffer.append(state)

    def recent_states(self):
        return np.array(self._state_buffer)

    def train(self, gamma: float = 0.99, mode: str = 'rgb_array'):

        # Initial States
        reward_sum = 0.
        q_mean = 0.
        target_mean = 0.

        while True:
            states = self.get_initial_states()
            losses = []
            checkpoint_flag = False
            target_update_flag = False
            play_steps = 0
            while True:
                # Get Action
                action: torch.LongTensor = self.select_action(states)

                for _ in range(self.frame_skipping):
                    # step 에서 나온 observation은 버림
                    observation, reward, done, info = self.env.step(action[0, 0])
                    next_state = self.env.get_screen()
                    self.add_state(next_state)

                    if done:
                        break

                # Store the infomation in Replay Memory
                next_states = self.recent_states()
                self.replay.put(states, action, reward, next_states)

                # Change States
                states = next_states

                # Optimize
                if self.replay.is_available():
                    loss, reward_sum, q_mean, target_mean = self.optimize(gamma)
                    losses.append(loss[0])

                if done:
                    break

                # Increase step
                self.step += 1
                play_steps += 1

                # Target Update
                if self.step % TARGET_UPDATE_INTERVAL == 0:
                    self._target_update()
                    target_update_flag = True

                # Checkpoint
                if self.step % CHECKPOINT_INTERVAL == 0:
                    self.save_checkpoint(filename=f'dqn_checkpoints/checkpoint_{self.step}.pth.tar')
                    checkpoint_flag = True

            self._play_steps.append(play_steps)

            # Logging
            mean_loss = np.mean(losses)
            target_update_msg = '  [target updated]' if target_update_flag else ''
            save_msg = '  [checkpoint!]' if checkpoint_flag else ''
            print(f'[{self.step}] Loss:{mean_loss:<8.4} Play:{play_steps:<3} AvgPlay:{self.play_step:<4.3} '
                  f'RewardSum:{reward_sum:<4} Q:{q_mean:<6.4} T:{target_mean:<6.4} '
                  f'Epsilon:{self.epsilon:<6.4}{target_update_msg}{save_msg}')

    def optimize(self, gamma: float):

        transitions = self.replay.sample(BATCH_SIZE)

        state_batch: Variable = Variable(torch.cat(transitions.state).cuda())
        action_batch: Variable = Variable(torch.cat(transitions.action).cuda())
        reward_batch: Variable = Variable(torch.cat(transitions.reward).cuda())
        next_state_batch: Variable = Variable(torch.cat(transitions.next_state).cuda())

        state_batch = state_batch.view([BATCH_SIZE, 3, self.action_repeat, self.env.width, self.env.height])
        next_state_batch = next_state_batch.view([BATCH_SIZE, 3, self.action_repeat, self.env.width, self.env.height])

        q_values = self.dqn(state_batch).gather(1, action_batch)
        target_values = self.target(next_state_batch).max(1)[0]

        loss = F.smooth_l1_loss(q_values, reward_batch + (target_values * gamma))

        self.optimizer.zero_grad()
        loss.backward()
        for param in self.dqn.parameters():
            param.grad.data.clamp_(-1, 1)
        self.optimizer.step()

        # print('dqn:', self._sum_params(self.dqn))
        # print('target:', self._sum_params(self.target))

        reward_score = int(torch.sum(reward_batch).data.cpu().numpy()[0])
        q_mean = torch.mean(q_values).data.cpu().numpy()[0]
        target_mean = torch.mean(target_values).data.cpu().numpy()[0]
        return loss.data.cpu().numpy(), reward_score, q_mean, target_mean

    def _target_update(self):
        self.target = copy.deepcopy(self.dqn)

    def save_checkpoint(self, filename='dqn_checkpoints/checkpoint.pth.tar'):
        checkpoint = {
            'dqn': self.dqn.state_dict(),
            'target': self.target.state_dict(),
            'optimizer': self.optimizer.state_dict(),
            'step': self.step
        }
        torch.save(checkpoint, filename)

    def load_checkpoint(self, filename='dqn_checkpoints/checkpoint.pth.tar', epsilon=None):
        checkpoint = torch.load(filename)
        self.dqn.load_state_dict(checkpoint['dqn'])
        self.target.load_state_dict(checkpoint['target'])
        self.optimizer.load_state_dict(checkpoint['optimizer'])
        self.step = checkpoint['step']

    def load_latest_checkpoint(self, epsilon=None):
        r = re.compile('checkpoint_(?P<number>\d+)\.pth\.tar$')
        files = glob.glob('dqn_checkpoints/checkpoint_*.pth.tar')
        if files:
            files = list(map(lambda x: [int(r.search(x).group('number')), x], files))
            files = sorted(files, key=lambda x: x[0])
            latest_file = files[-1][1]
            self.load_checkpoint(latest_file, epsilon=epsilon)
            print(f'latest checkpoint has been loaded - {latest_file}')
        else:
            print('no latest checkpoint')

    def play(self):
        observation = self.env.game.reset()
        states = self.get_initial_states()
        while True:
            screen = self.env.game.render(mode='human')

            states = states.reshape(1, 3, self.action_repeat, self.env.width, self.env.height)
            states_variable: Variable = Variable(torch.FloatTensor(states).cuda())
            action = self.dqn(states_variable).data.cpu().max(1)[1][0, 0]

            observation, reward, done, info = self.env.step(action)

            print(f'action:{action}, reward:{reward}')

            next_state = self.env.get_screen()
            self.add_state(next_state)
            states = self.recent_states()

            if done:
                break
        self.env.game.close()

    @property
    def play_step(self):
        return np.mean(self._play_steps)

    def _sum_params(self, model):
        return np.sum([torch.sum(p).data[0] for p in model.parameters()])

    def imshow(self, sample_image: np.array, transpose=True):
        if transpose:
            sample_image = sample_image.transpose((1, 2, 0))
        pylab.imshow(sample_image)
        pylab.show()


def main():
    args = parser.parse_args()

    agent = Agent(args.game)
    if args.load_latest and not args.checkpoint:
        agent.load_latest_checkpoint()
    elif args.checkpoint:
        agent.load_checkpoint(args.checkpoint)

    if args.mode.lower() == 'play':
        agent.play()
    elif args.mode.lower() == 'train':
        agent.train()


if __name__ == '__main__':
    main()
