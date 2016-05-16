#!/usr/bin/env python2
# -*- coding: utf-8 -*-
# File: DQN.py
# Author: Yuxin Wu <ppwwyyxx@gmail.com>

import tensorflow as tf
import numpy as np
import os, sys
import random
import argparse
from tqdm import tqdm
import multiprocessing

from tensorpack import *
from tensorpack.models import  *
from tensorpack.utils import  *
from tensorpack.utils.concurrency import ensure_proc_terminate
from tensorpack.utils.stat import  *
from tensorpack.predict import PredictConfig, get_predict_func, ParallelPredictWorker
from tensorpack.tfutils import symbolic_functions as symbf
from tensorpack.callbacks import *

from tensorpack.dataflow.dataset import AtariDriver, AtariPlayer
from exp_replay import AtariExpReplay

"""
Implement DQN in:
Human-level control through deep reinforcement learning
for atari games
"""

BATCH_SIZE = 32
IMAGE_SIZE = 84
NUM_ACTIONS = None
FRAME_HISTORY = 4
ACTION_REPEAT = 3
GAMMA = 0.99
BATCH_SIZE = 32

INIT_EXPLORATION = 1
EXPLORATION_EPOCH_ANNEAL = 0.0025
END_EXPLORATION = 0.1

INIT_MEMORY_SIZE = 50000
MEMORY_SIZE = 1e6


class Model(ModelDesc):
    def _get_input_vars(self):
        assert NUM_ACTIONS is not None
        return [InputVar(tf.float32, (None, IMAGE_SIZE, IMAGE_SIZE, FRAME_HISTORY), 'state'),
                InputVar(tf.int32, (None,), 'action'),
                InputVar(tf.float32, (None,), 'reward'),
                InputVar(tf.float32, (None, IMAGE_SIZE, IMAGE_SIZE, FRAME_HISTORY), 'next_state'),
                InputVar(tf.bool, (None,), 'isOver')
                ]

    def _get_DQN_prediction(self, image, is_training):
        """ image: [0,255]"""
        image = image / 128.0 - 1
        with argscope(Conv2D, nl=tf.nn.relu, use_bias=True):
            l = Conv2D('conv0', image, out_channel=32, kernel_shape=5, stride=2)
            l = Conv2D('conv1', l, out_channel=32, kernel_shape=5, stride=2)
            l = Conv2D('conv2', l, out_channel=64, kernel_shape=4, stride=2)
            l = Conv2D('conv3', l, out_channel=64, kernel_shape=3)

        l = FullyConnected('fc0', l, 512)
        l = FullyConnected('fct', l, out_dim=NUM_ACTIONS, nl=tf.identity, summary_activation=False)
        return l

    def _build_graph(self, inputs, is_training):
        state, action, reward, next_state, isOver = inputs
        self.predict_value = self._get_DQN_prediction(state, is_training)
        action_onehot = symbf.one_hot(action, NUM_ACTIONS)
        pred_action_value = tf.reduce_sum(self.predict_value * action_onehot, 1)    #Nx1
        max_pred_reward = tf.reduce_mean(tf.reduce_max(
            self.predict_value, 1), name='predict_reward')
        tf.add_to_collection(MOVING_SUMMARY_VARS_KEY, max_pred_reward)

        with tf.variable_scope('target'):
            targetQ_predict_value = tf.stop_gradient(
                    self._get_DQN_prediction(next_state, False))    # NxA
            target = tf.select(isOver, reward, reward +
                    GAMMA * tf.reduce_max(targetQ_predict_value, 1))    # Nx1

        sqrcost = tf.square(target - pred_action_value)
        abscost = tf.abs(target - pred_action_value)    # robust error func
        cost = tf.select(abscost < 1, sqrcost, abscost)
        summary.add_param_summary([('.*/W', ['histogram'])])   # monitor histogram of all W
        self.cost = tf.reduce_mean(cost, name='cost')

    def update_target_param(self):
        vars = tf.trainable_variables()
        ops = []
        for v in vars:
            target_name = v.op.name
            if target_name.startswith('target'):
                new_name = target_name.replace('target/', '')
                logger.info("{} <- {}".format(target_name, new_name))
                ops.append(v.assign(tf.get_default_graph().get_tensor_by_name(new_name + ':0')))
        return tf.group(*ops)

    def get_gradient_processor(self):
        return [MapGradient(lambda grad: \
                tf.clip_by_global_norm([grad], 5)[0][0]),
                SummaryGradient()]

def current_predictor(state):
    pred_var = tf.get_default_graph().get_tensor_by_name('fct/output:0')
    pred = pred_var.eval(feed_dict={'state:0': [state]})
    return pred[0]

class TargetNetworkUpdator(Callback):
    def __init__(self, M):
        self.M = M

    def _setup_graph(self):
        self.update_op = self.M.update_target_param()

    def _update(self):
        logger.info("Delayed Predictor updating...")
        self.update_op.run()

    def _before_train(self):
        self._update()

    def _trigger_epoch(self):
        self._update()

class ExpReplayController(Callback):
    def __init__(self, d):
        self.d = d

    def _before_train(self):
        self.d.init_memory()

    def _trigger_epoch(self):
        if self.d.exploration > END_EXPLORATION:
            self.d.exploration -= EXPLORATION_EPOCH_ANNEAL
            logger.info("Exploration: {}".format(self.d.exploration))

def play_model(model_path, romfile):
    player = AtariPlayer(AtariDriver(romfile, viz=0.01),
            action_repeat=ACTION_REPEAT)
    global NUM_ACTIONS
    NUM_ACTIONS = player.driver.get_num_actions()

    M = Model()
    cfg = PredictConfig(
            model=M,
            input_data_mapping=[0],
            session_init=SaverRestore(model_path),
            output_var_names=['fct/output:0'])
    predfunc = get_predict_func(cfg)
    tot_reward = 0
    while True:
        s = player.current_state()
        outputs = predfunc([[s]])
        action_value = outputs[0][0]
        act = action_value.argmax()
        print action_value, act
        if random.random() < 0.01:
            act = random.choice(range(player.driver.get_num_actions()))
        print(act)
        _, reward, isOver = player.action(act)
        tot_reward += reward
        if isOver:
            print("Total:", tot_reward)
            tot_reward = 0
            pbar.update()

def eval_model_multiprocess(model_path, romfile):
    M = Model()
    cfg = PredictConfig(
            model=M,
            input_data_mapping=[0],
            session_init=SaverRestore(model_path),
            output_var_names=['fct/output:0'])

    class Worker(ParallelPredictWorker):
        def __init__(self, idx, gpuid, config, outqueue):
            super(Worker, self).__init__(idx, gpuid, config)
            self.outq = outqueue

        def run(self):
            player = AtariPlayer(AtariDriver(romfile, viz=0),
                    action_repeat=ACTION_REPEAT)
            global NUM_ACTIONS
            NUM_ACTIONS = player.driver.get_num_actions()

            self._init_runtime()

            tot_reward = 0
            while True:
                s = player.current_state()
                outputs = self.func([[s]])
                action_value = outputs[0][0]
                act = action_value.argmax()
                #print action_value, act
                if random.random() < 0.01:
                    act = random.choice(range(player.driver.get_num_actions()))
                #print(act)
                _, reward, isOver = player.action(act)
                tot_reward += reward
                if isOver:
                    self.outq.put(tot_reward)
                    tot_reward = 0

    NR_PROC = multiprocessing.cpu_count() // 2
    procs = []
    q = multiprocessing.Queue()
    for k in range(NR_PROC):
        procs.append(Worker(k, -1, cfg, q))
    ensure_proc_terminate(procs)
    for k in procs:
        k.start()
    stat = StatCounter()
    EVAL_EPISODE = 50
    with tqdm(total=EVAL_EPISODE) as pbar:
        while True:
            r = q.get()
            stat.feed(r)
            pbar.update()
            if stat.count() == EVAL_EPISODE:
                logger.info("Average Score: {}. Max Score: {}".format(
                    stat.average, stat.max))
                break


def get_config(romfile):
    basename = os.path.basename(__file__)
    logger.set_logger_dir(
        os.path.join('train_log', basename[:basename.rfind('.')]))
    M = Model()

    driver = AtariDriver(romfile)
    global NUM_ACTIONS
    NUM_ACTIONS = driver.get_num_actions()

    dataset_train = AtariExpReplay(
            predictor=current_predictor,
            player=AtariPlayer(
                driver, hist_len=FRAME_HISTORY,
                action_repeat=ACTION_REPEAT),
            memory_size=MEMORY_SIZE,
            batch_size=BATCH_SIZE,
            populate_size=INIT_MEMORY_SIZE,
            exploration=INIT_EXPLORATION)

    lr = tf.Variable(0.0025, trainable=False, name='learning_rate')
    tf.scalar_summary('learning_rate', lr)

    return TrainConfig(
        dataset=dataset_train,
        optimizer=tf.train.AdamOptimizer(lr, epsilon=1e-3),
        callbacks=Callbacks([
            StatPrinter(),
            ModelSaver(),
            HumanHyperParamSetter('learning_rate', 'hyper.txt'),
            HumanHyperParamSetter((dataset_train, 'exploration'), 'hyper.txt'),
            TargetNetworkUpdator(M),
            ExpReplayController(dataset_train)
        ]),
        session_config=get_default_sess_config(0.5),
        model=M,
        step_per_epoch=10000,
        max_epoch=10000,
    )

if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--gpu', help='comma separated list of GPU(s) to use.') # nargs='*' in multi mode
    parser.add_argument('--load', help='load model')
    parser.add_argument('--task', help='task to perform',
            choices=['play', 'eval', 'train'], default='train')
    parser.add_argument('--rom', help='atari rom', required=True)
    args = parser.parse_args()

    if args.gpu:
        os.environ['CUDA_VISIBLE_DEVICES'] = args.gpu

    if args.task != 'train':
        assert args.load is not None

    if args.task == 'play':
        play_model(args.load, args.rom)
        sys.exit()
    if args.task == 'eval':
        eval_model_multiprocess(args.load, args.rom)
        sys.exit()

    with tf.Graph().as_default():
        config = get_config(args.rom)
        if args.load:
            config.session_init = SaverRestore(args.load)
        SimpleTrainer(config).train()

