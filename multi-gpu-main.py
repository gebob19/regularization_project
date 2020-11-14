#%%
import tensorflow as tf 
import numpy as np
import imageio
import cv2 
from writers import NeptuneWriter
from models import * 
from config import * 
from tqdm import tqdm 

print(tf.__version__)
N_GPUS = len(tf.config.experimental.list_physical_devices('GPU'))
print("Num GPUs Available: ", N_GPUS)

#%%
def mean_over_dict(custom_metrics):
    mean_metrics = {}
    for k in custom_metrics.keys(): 
        mean_metrics[k] = np.mean(custom_metrics[k])
    return mean_metrics

def init_metrics():
    metrics = {'train_acc': [], 'val_acc': [], 'train_loss': [], 'val_loss': []}
    return metrics

def inf(): 
    i = 0 
    while True: 
        yield i
        i += 1 

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


def train(trainer):
    with tf.device('/cpu:0'):
        # MULTI-GPU SETUP 
        loss_tensors, accuracy_tensors = [], []

        tower_grads = []
        with i in range(N_GPUS):
            with tf.device('/gpu:{}'.format(i)):
                with tf.name_scope('GPU_{}'.format(i)) as scope:
                    tower_grads.append(trainer.grads)
                    loss_tensors.append(trainer.loss)
                    accuracy_tensors.append(trainer.acc)

        grads_and_vars = average_gradients(tower_grads)
        apply_grads = trainer.optimizer.apply_gradients(grads_and_vars)
        train_op = tf.group(apply_grads, name='train_op')

        avg_loss = tf.reduce_mean(loss_tensors)
        avg_acc = tf.reduce_mean(accuracy_tensors)

        ## softplacement for placement across available GPUs
        with tf.compat.v1.Session(config=tf.ConfigProto(allow_soft_placement=True)) as sess:
            best_sess = sess
            best_score = 0. 
            last_improvement = 0
            stop = False 
            step = 0

            sess.run([tf.compat.v1.global_variables_initializer(), \
                tf.compat.v1.local_variables_initializer()])

            train_handle_value, val_handle_value, test_handle_value = \
                sess.run([trainer.train_handle, trainer.val_handle, trainer.test_handle])

            for e in range(EPOCHS):
                metrics = init_metrics()

                sess.run([trainer.train_iterator.initializer, \
                    trainer.val_iterator.initializer, trainer.test_iterator.initializer])

                # training 
                try: 
                    print('Training...')
                    sess.run(trainer.acc_initializer) # reset accuracy metric
                    for _ in tqdm(inf()):
                        ## RUN SESS WITH MULTI-GPU VALUES 
                        _, loss, acc = sess.run([train_op, avg_loss, \
                                    avg_acc], \
                                    feed_dict={trainer.handle_flag: train_handle_value,
                                    trainer.is_training: True})
                        metrics['train_loss'].append(loss)
                        metrics['train_acc'].append(acc)

                        step += 1
                        if step % 50 == 0: 
                            mean_metrics = mean_over_dict(metrics)
                            writer.write(mean_metrics, i)
                            metrics = init_metrics()
                        
                        if TRIAL_RUN: break 
                except tf.errors.OutOfRangeError: pass 

                # validation 
                try: 
                    sess.run(trainer.acc_initializer) # reset accuracy metric
                    for i in tqdm(inf()):
                        loss, _ = sess.run([trainer.loss, trainer.acc_op], \
                            feed_dict={trainer.handle_flag: val_handle_value, 
                            trainer.is_training: False})        
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
                writer.write(mean_metrics, e)

                print("{} {}".format(e, mean_metrics))

                if stop: 
                    print('Early stopping...')
                    break 

            # test 
            try: 
                sess = best_sess # restore session with the best score
                sess.run(trainer.acc_initializer) # reset accuracy metric
                while True:
                    sess.run([trainer.acc_op], \
                        feed_dict={
                            trainer.handle_flag: test_handle_value, 
                            trainer.is_training: False})        
                    if TRIAL_RUN: break 
            except tf.errors.OutOfRangeError: pass 

            test_acc = sess.run(trainer.acc)
            writer.write({'test_acc': test_acc}, e+1)
            print('test_accuracy: ', test_acc)

            writer.fin()
    return trainer 

#%%
TRIAL_RUN = False
writer = NeptuneWriter('gebob19/672-asl')

EPOCHS = 100 if not TRIAL_RUN else 1
BATCH_SIZE = BATCH_SIZE if not TRIAL_RUN else 32
PREFETCH_BUFFER = PREFETCH_BUFFER if not TRIAL_RUN else 32
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

trainers = [Baseline]
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