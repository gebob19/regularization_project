#%%
import tensorflow as tf 
import numpy as np
import imageio
import cv2 
import random 
from writers import NeptuneWriter
from models import * 
from config import * 
from tqdm import tqdm 

print(tf.__version__)
print("Num GPUs Available: ", N_GPUS)

#%%
def get_train_test(batch_size=32):
    (x_train, y_train), (x_test, y_test) = tf.keras.datasets.mnist.load_data()
    x_train, x_test = x_train[:, :, :, None].astype(np.float32), x_test[:, :, :, None].astype(np.float32)

    # x_train = np.reshape(x_train, (-1, 784)).astype(np.float32)
    # x_test = np.reshape(x_test, (-1, 784)).astype(np.float32)

    # one-hot encoding 
    oh = np.zeros((y_train.size, 10))
    oh[np.arange(y_train.size), y_train] = 1 
    y_train = oh

    oh = np.zeros((y_test.size, 10))
    oh[np.arange(y_test.size), y_test] = 1 
    y_test = oh

    # Reserve 10,000 samples for validation.
    x_val = x_train[-10000:]
    y_val = y_train[-10000:]
    y_train = y_train[:-10000]
    x_train = x_train[:-10000]

    return (x_train, y_train), (x_val, y_val), (x_test, y_test)


#%%
def shuffle_and_overwrite(file_name):
    content = open(file_name, 'r').readlines()
    random.shuffle(content)
    with open(file_name, 'w') as f:
        for line in content:
            f.write(line)

def mean_over_dict(custom_metrics):
    mean_metrics = {}
    for k in custom_metrics.keys(): 
        if len(custom_metrics[k]) > 0:
            mean_metrics[k] = np.mean(custom_metrics[k])
    return mean_metrics

def init_metrics():
    metrics = {'train_acc': [], 'val_acc': [], 'train_loss': [], 'val_loss': []}
    return metrics

## FROM https://github.com/tensorflow/tensorflow/blob/r0.7/tensorflow/models/image/cifar10/cifar10.py
def average_gradients(tower_grads):
    average_grads = []
    for grad_and_vars in zip(*tower_grads):
        grads = []
        for g, _ in grad_and_vars:
            expanded_g = tf.expand_dims(g, 0)
            grads.append(expanded_g)
        grad = tf.concat(grads, 0)
        grad = tf.reduce_mean(grad, 0)
        v = grad_and_vars[0][1]  # var
        grad_and_var = (grad, v)
        average_grads.append(grad_and_var)
    return average_grads


def inf(): 
    i = 0 
    while True: 
        yield i
        i += 1 

def train(trainer):
    multi_gpu = True if N_GPUS > 1 else False
    multi_gpu = False 
    if multi_gpu:
        print('Using Multi-GPU setup...')
        with tf.device('/cpu:0'):
            # MULTI-GPU SETUP 
            loss_tensors, accuracy_tensors = [], []

            xb, yb, _, _ = trainer.dataset_iterator.get_next()
            xb.set_shape([None, 10, IMAGE_SIZE_H, IMAGE_SIZE_W, 3])

            def compute_loss_acc(xb, yb):
                logits = trainer.model(xb)
                loss = trainer.loss_func(yb, logits)
                acc, _ = tf.compat.v1.metrics.accuracy(tf.argmax(yb, 1), tf.argmax(logits, 1), name='acc')
                return loss, acc

            tower_grads = []
            for i in range(N_GPUS):
                with tf.device('/gpu:{}'.format(i)):
                    with tf.name_scope('GPU_{}'.format(i)) as scope:
                        loss, acc = compute_loss_acc(xb, yb)
                        grads = trainer.optimizer.compute_gradients(loss)

                        tower_grads.append(grads)
                        loss_tensors.append(loss)
                        accuracy_tensors.append(acc)
                        update_ops = tf.get_collection(tf.GraphKeys.UPDATE_OPS, scope)

            grads = average_gradients(tower_grads)
            apply_gradient_op = trainer.optimizer.apply_gradients(grads)

            with tf.control_dependencies(update_ops):
                train_op = tf.group(apply_gradient_op, name='train_op')

            avg_loss = tf.reduce_mean(loss_tensors)
            avg_acc = tf.reduce_mean(accuracy_tensors)
    else: 
        print('Not using Multi-GPU setup...')

    # mnist test 
    (x_train, y_train), (x_val, y_val), (x_test, y_test) = get_train_test()

    with tf.compat.v1.Session(config=tf.compat.v1.ConfigProto(allow_soft_placement=True)) as sess:
        best_sess = sess
        best_score = 0. 
        last_improvement = 0
        stop = False 
        step = 0

        sess.run([tf.compat.v1.global_variables_initializer(), \
            tf.compat.v1.local_variables_initializer()])

        # train_handle_value, val_handle_value, test_handle_value = \
        #     sess.run([trainer.train_handle, trainer.val_handle, trainer.test_handle])

        for e in range(EPOCHS):
            metrics = init_metrics()
            
            # sess.run([trainer.train_iterator.initializer, \
            #     trainer.val_iterator.initializer, trainer.test_iterator.initializer])

            # training 
            sess.run(trainer.dset_init, \
                feed_dict={trainer.x_data: x_train, trainer.y_data: y_train})
            try: 
                print('Training...')
                sess.run(trainer.acc_initializer) # reset accuracy metric
                for _ in tqdm(inf()):

                    if multi_gpu: 
                        ## RUN SESS WITH MULTI-GPU VALUES 
                        _, loss, acc = sess.run([train_op, avg_loss, \
                                    avg_acc], \
                                    feed_dict={trainer.handle_flag: train_handle_value,
                                    trainer.is_training: True})
                        metrics['train_acc'].append(acc)
                    else: 
                        _, loss, _ = sess.run([trainer.train_op, trainer.loss, trainer.acc_op])
                        # _, loss, _ = sess.run([trainer.train_op, trainer.loss, \
                        #             trainer.acc_op], \
                        #             feed_dict={trainer.handle_flag: train_handle_value,
                        #             trainer.is_training: True})

                    metrics['train_loss'].append(loss)

                    step += 1
                    # if step % 50 == 0: 
                    #     if not multi_gpu: 
                    #         metrics['train_acc'] = [sess.run(trainer.acc)]
                    #         sess.run(trainer.acc_initializer) # reset accuracy metric
                    #     mean_metrics = mean_over_dict(metrics)
                    #     writer.write(mean_metrics, step)
                    #     metrics = init_metrics()
                    
                    if TRIAL_RUN: break 
            except tf.errors.OutOfRangeError: pass 
            metrics['train_acc'] = [sess.run(trainer.acc)]

            # validation -- always single GPU for simplicity 
            sess.run(trainer.acc_initializer) # reset accuracy metric
            sess.run(trainer.dset_init, feed_dict={
                trainer.x_data: x_val, 
                trainer.y_data: y_val})
            try: 
                sess.run(trainer.acc_initializer) # reset accuracy metric
                for i in tqdm(inf()):
                    loss, _ = sess.run([trainer.loss, trainer.acc_op])        
                    # loss, _ = sess.run([trainer.loss, trainer.acc_op], \
                    #     feed_dict={trainer.handle_flag: val_handle_value, 
                    #     trainer.is_training: False})        
                    metrics['val_loss'].append(loss)
                    
                    if TRIAL_RUN: break 
            except tf.errors.OutOfRangeError: pass 
            val_acc = sess.run(trainer.acc)
            metrics['val_acc'] = [val_acc]

            # early stopping
            ## https://stackoverflow.com/questions/46428604/how-to-implement-early-stopping-in-tensorflow
            if val_acc > best_score:
                best_sess = sess # save session
                best_score = val_acc
            else:
                last_improvement += 1
            if last_improvement > REQUIRED_IMPROVEMENT:
                # Break out from the loop.
                stop = True

            mean_metrics = mean_over_dict(metrics)
            writer.write(mean_metrics, step)

            print("{} {}".format(e, mean_metrics))

            if stop: 
                print('Early stopping...')
                break 

        # # test -- also single GPU  
        # try: 
        #     sess = best_sess # restore session with the best score
        #     sess.run(trainer.acc_initializer) # reset accuracy metric
        #     while True:
        #         sess.run([trainer.acc_op], \
        #             feed_dict={
        #                 trainer.handle_flag: test_handle_value, 
        #                 trainer.is_training: False})        
        #         if TRIAL_RUN: break 
        # except tf.errors.OutOfRangeError: pass 

        # test_acc = sess.run(trainer.acc)
        # writer.write({'test_acc': test_acc}, e+1)
        # print('test_accuracy: ', test_acc)

        writer.fin()
    return trainer 

#%%
TRIAL_RUN = True
writer = NeptuneWriter('gebob19/672-asl')

EPOCHS = 100 if not TRIAL_RUN else 1
BATCH_SIZE = BATCH_SIZE if not TRIAL_RUN else 2
PREFETCH_BUFFER = PREFETCH_BUFFER if not TRIAL_RUN else 2
REQUIRED_IMPROVEMENT = 10

config = {
    'EPOCHS': EPOCHS,
    'BATCH_SIZE': BATCH_SIZE, 
    'IMAGE_SIZE_H': IMAGE_SIZE_H,
    'IMAGE_SIZE_W': IMAGE_SIZE_W,
    'PREFETCH_BUFFER': PREFETCH_BUFFER,
    'NUM_CLASSES': NUM_CLASSES,
    'DROPOUT_CONSTANT': 0.5,
    'REG_CONSTANT': 0.01, 
    'REQUIRED_IMPROVEMENT': REQUIRED_IMPROVEMENT,
 }

# default configs 
trainers = [Baseline, L1Reg, L2Reg, Dropout, SpectralReg, OrthogonalReg]
configs = [config.copy(), config.copy(), config.copy(), config.copy(), config.copy(), config.copy()]

# variations of regularization/dropout parameters 
new_trainers, new_configs = [], []
for config, trainer_class in zip(configs, trainers):
    if trainer_class.__name__ == 'Baseline': 
        continue

    if trainer_class.__name__ == 'OrthogonalReg':
        new_confg = config.copy()
        new_confg['REG_CONSTANT'] = 0.1
        new_configs.append(new_confg)
        new_trainers.append(trainer_class)
        
        new_confg = config.copy()
        new_confg['REG_CONSTANT'] = 0.001
        new_configs.append(new_confg)
        new_trainers.append(trainer_class)
        
        new_confg = config.copy()
        new_confg['REG_CONSTANT'] = 0.0001
        new_configs.append(new_confg)
        new_trainers.append(trainer_class)

    elif trainer_class.__name__ != 'DropoutReg':
        new_confg = config.copy()
        new_confg['REG_CONSTANT'] *= 10
        new_configs.append(new_confg)
        new_trainers.append(trainer_class)

        new_confg = config.copy()
        new_confg['REG_CONSTANT'] *= 100
        new_configs.append(new_confg)
        new_trainers.append(trainer_class)

        new_confg = config.copy()
        new_confg['REG_CONSTANT'] /= 10
        new_configs.append(new_confg)
        new_trainers.append(trainer_class)

        new_confg = config.copy()
        new_confg['REG_CONSTANT'] /= 100
        new_configs.append(new_confg)
        new_trainers.append(trainer_class)
    else: 
        new_confg = config.copy()
        new_confg['DROPOUT_CONSTANT'] = 0.5
        new_configs.append(new_confg)
        new_trainers.append(trainer_class)

        new_confg = config.copy()
        new_confg['DROPOUT_CONSTANT'] = 0.8
        new_configs.append(new_confg)
        new_trainers.append(trainer_class)

        new_confg = config.copy()
        new_confg['DROPOUT_CONSTANT'] = 0.1
        new_configs.append(new_confg)
        new_trainers.append(trainer_class)
        
trainers += new_trainers
configs += new_configs

# if TRIAL_RUN:
trainers = [Baseline2D]
configs = [config]

for config, trainer_class in zip(configs, trainers): 
    config['experiment_name'] = trainer_class.__name__
    if not TRIAL_RUN:
        writer.start(config)

    tf.compat.v1.reset_default_graph()
    trainer = trainer_class(config)
    full_trainer = train(trainer)
    
    writer.fin()

print('Complete!')
