import json
import os
import time
import re
import numpy as np
import tensorflow as tf
import pandas as pd
import seaborn as sns
import matplotlib
import matplotlib.pyplot as plt
from collections import defaultdict

import keras
import keras.backend as K
from keras.callbacks import EarlyStopping, ModelCheckpoint, ReduceLROnPlateau, CSVLogger
from keras.layers import merge, recurrent, Dense, Input, Dropout, TimeDistributed
from keras.layers.embeddings import Embedding
from keras.layers.normalization import BatchNormalization
from keras.layers.core import Lambda
from keras.layers.wrappers import Bidirectional
from keras.models import Model
from keras.preprocessing.sequence import pad_sequences
from keras.preprocessing.text import Tokenizer
from keras.regularizers import l2
from keras.utils import np_utils
from keras.layers.recurrent import GRU,LSTM
from keras.backend.tensorflow_backend import set_session
from keras.engine.topology import Layer
# import h5py #: requires h5py to store parameters !


def time_count(fn):
  # Funtion wrapper used to memsure time consumption
  def _wrapper(*args, **kwargs):
    start = time.clock()
    fn(*args, **kwargs)
    print("[time_count]: %s cost %fs" % (fn.__name__, time.clock() - start))
  return _wrapper



class AttentionAlignmentModel:

  def __init__(self, annotation ='biGRU'):
    # 1, Set Basic Model Parameters
    self.Layers = 1
    self.EmbeddingSize = 300
    self.BatchSize = 512
    self.Patience = 8
    self.MaxEpoch = 64
    self.SentMaxLen = 36
    self.DropProb = 0.2
    self.L2Strength = 2e-6
    self.Activate = 'relu'
    self.Optimizer = 'rmsprop'
    self.rnn_type = annotation

    # 2, Define Class Variables
    self.Vocab = 0
    self.model = None
    self.GloVe = defaultdict(np.array)
    self.indexer,self.Embed = None, None
    self.train, self.validation, self.test = [],[],[]
    self.Labels = {'contradiction': 0, 'neutral': 1, 'entailment': 2}
    self.rLabels = {0:'contradiction', 1:'neutral', 2:'entailment'}

  @staticmethod
  def load_data():
    trn = json.loads(open('train.json', 'r').read())
    vld = json.loads(open('validation.json', 'r').read())
    tst = json.loads(open('test.json', 'r').read())

    trn[2] = np_utils.to_categorical(trn[2], 3)
    vld[2] = np_utils.to_categorical(vld[2], 3)
    tst[2] = np_utils.to_categorical(tst[2], 3)
    return trn, vld, tst

  @time_count
  def prep_data(self,fn=('train.json','validation.json','test.json')):
    # 1, Read raw Training,Validation and Test data
    self.train,self.validation,self.test = self.load_data()

    # 2, Prep Word Indexer: assign each word a number
    self.indexer = Tokenizer(lower=False, filters='')
    self.indexer.fit_on_texts(self.train[0] + self.train[1])
    self.Vocab = len(self.indexer.word_counts) + 1

    # 3, Convert each word in sent to num and zero pad
    def padding(x, MaxLen):
      return pad_sequences(sequences=self.indexer.texts_to_sequences(x), maxlen=MaxLen)
    def pad_data(x):
      return padding(x[0], self.SentMaxLen), padding(x[1], self.SentMaxLen), x[2]

    self.train = pad_data(self.train)
    self.validation = pad_data(self.validation)
    self.test = pad_data(self.test)

  def load_GloVe(self):
    # Creat a embedding matrix for word2vec(use GloVe)
    embed_index = {}
    for line in open('glove.840B.300d.txt','r'):
      value = line.split(' ') # Warning: Can't use split()! I don't know why...
      word = value[0]
      embed_index[word] = np.asarray(value[1:],dtype='float32')
    embed_matrix = np.zeros((self.Vocab,self.EmbeddingSize))
    unregistered = []
    for word,i in self.indexer.word_index.items():
      vec = embed_index.get(word)
      if vec is None: unregistered.append(word)
      else: embed_matrix[i] = vec
    np.save('GloVe.npy',embed_matrix)
    open('unregisterd_word.txt','w').write(str(unregistered))

  def load_GloVe_dict(self):
    for line in open('glove.840B.300d.txt','r'):
      value = line.split(' ') # Warning: Can't use split()! I don't know why...
      word = value[0]
      self.GloVe[word] = np.asarray(value[1:],dtype='float32')

  @time_count
  def prep_embd(self):
    # Add a Embed Layer to convert word index to vector
    if not os.path.exists('GloVe.npy'):
      self.load_GloVe()
    embed_matrix = np.load('GloVe.npy')
    self.Embed = Embedding(input_dim = self.Vocab,
                           output_dim = self.EmbeddingSize,
                           input_length = self.SentMaxLen,
                           trainable = False,
                           weights = [embed_matrix])


  def create_model(self, test_mode = False):
    ''' This model is Largely based on [A Decomposable Attention Model, Ankur et al.] '''
    # 0, (Optional) Set the upper limit of GPU memory
    config = tf.ConfigProto()
    config.gpu_options.per_process_gpu_memory_fraction = 0.2
    set_session(tf.Session(config=config))

    # 1, Embedding the input and project the embeddings
    premise = Input(shape=(self.SentMaxLen,), dtype='int32')
    hypothesis = Input(shape=(self.SentMaxLen,), dtype='int32')
    embed_p = self.Embed(premise) # [batchsize, Psize, Embedsize]
    embed_h = self.Embed(hypothesis) # [batchsize, Hsize, Embedsize]
    EmbdProject = TimeDistributed(Dense(200,
                                   activation='relu',
                                   kernel_regularizer=l2(self.L2Strength),
                                   bias_regularizer=l2(self.L2Strength)))
    embed_p = Dropout(self.DropProb)(EmbdProject(embed_p)) # [batchsize, Psize, units]
    embed_h = Dropout(self.DropProb)(EmbdProject(embed_h)) # [batchsize, Hsize, units]

    # 2, Score each embeddings and calc score matrix Eph.
    F_p, F_h = embed_p, embed_h
    for i in range(2): # Applying Decomposable Score Function
      scoreF = TimeDistributed(Dense(200,
                                     activation='relu',
                                     kernel_regularizer=l2(self.L2Strength),
                                     bias_regularizer=l2(self.L2Strength)))
      F_p = Dropout(self.DropProb)(scoreF(F_p)) # [batch_size, Psize, units]
      F_h = Dropout(self.DropProb)(scoreF(F_h)) # [batch_size, Hsize, units]
    Eph = keras.layers.Dot(axes=(2, 2))([F_p, F_h]) # [batch_size, Psize, Hsize]

    # 3, Normalize score matrix and get alignment
    Ep = Lambda(lambda x:keras.activations.softmax(x))(Eph) # [batch_size, Psize, Hsize]
    Eh = keras.layers.Permute((2, 1))(Eph) # [batch_size, Hsize, Psize)
    Eh = Lambda(lambda x:keras.activations.softmax(x))(Eh) # [batch_size, Hsize, Psize]
    PremAlign = keras.layers.Dot((2, 1))([Ep, embed_h])
    HypoAlign = keras.layers.Dot((2, 1))([Eh, embed_p])

    # 4, Concat original and alignment, score each pair of alignment
    PremAlign = keras.layers.concatenate([embed_p, PremAlign]) # [batch_size, PreLen, 2*Size]
    HypoAlign = keras.layers.concatenate([embed_h, HypoAlign])# [batch_size, Hypo, 2*Size]
    for i in range(2):
      scoreG = TimeDistributed(Dense(200,
                                     activation='relu',
                                     kernel_regularizer=l2(self.L2Strength),
                                     bias_regularizer=l2(self.L2Strength)))
      PremAlign = scoreG(PremAlign) # [batch_size, Psize, units]
      HypoAlign = scoreG(HypoAlign) # [batch_size, Hsize, units]
      PremAlign = Dropout(self.DropProb)(PremAlign)
      HypoAlign = Dropout(self.DropProb)(HypoAlign)

    # 5, Sum all these scores, and make final judge according to sumed-score
    SumWords = Lambda(lambda X: K.reshape(K.sum(X, axis=1, keepdims=True), (-1, 200)))
    V_P = SumWords(PremAlign) # [batch_size, 512]
    V_H = SumWords(HypoAlign) # [batch_size, 512]
    final = keras.layers.concatenate([V_P, V_H])
    for i in range(2):
      final = Dense(200,
                    activation='relu',
                    kernel_regularizer=l2(self.L2Strength),
                    bias_regularizer=l2(self.L2Strength))(final)
      final = Dropout(self.DropProb)(final)
      final = BatchNormalization()(final)

    # 6, Prediction by softmax
    final = Dense(3, activation='softmax')(final)
    if test_mode: self.model = Model(inputs=[premise,hypothesis],outputs=[Ep, Eh, final])
    else: self.model = Model(inputs=[premise, hypothesis], outputs=final)



  def create_model2(self, returnEpEh = False):
    # 0, (Optional) Set the upper limit of GPU memory
    config = tf.ConfigProto()
    config.gpu_options.per_process_gpu_memory_fraction = 0.2
    set_session(tf.Session(config=config))

    # 1, Embedding the input and project the embeddings
    premise = Input(shape=(self.SentMaxLen,), dtype='int32')
    hypothesis = Input(shape=(self.SentMaxLen,), dtype='int32')
    embed_p = self.Embed(premise)  # [batchsize, Psize, Embedsize]
    embed_h = self.Embed(hypothesis)  # [batchsize, Hsize, Embedsize]
    EmbdProject = TimeDistributed(Dense(200,
                                        activation='relu',
                                        kernel_regularizer=l2(self.L2Strength),
                                        bias_regularizer=l2(self.L2Strength)))
    embed_p = EmbdProject(embed_p)  # [batchsize, Psize, units]
    embed_h = EmbdProject(embed_h)  # [batchsize, Hsize, units]

    # 2, Score each embeddings and calc score matrix Eph.
    F_p, F_h = embed_p, embed_h
    for i in range(2):  # Applying Decomposable Score Function
      scoreF = TimeDistributed(Dense(200,
                                     activation='relu',
                                     kernel_regularizer=l2(self.L2Strength),
                                     bias_regularizer=l2(self.L2Strength)))
      F_p = Dropout(self.DropProb / 2)(scoreF(F_p))  # [batch_size, Psize, units]
      F_h = Dropout(self.DropProb / 2)(scoreF(F_h))  # [batch_size, Hsize, units]
    Eph = keras.layers.Dot(axes=(2, 2))([F_h, F_p])  # [batch_size, Psize, Hsize]

    # 3, Normalize score matrix and get alignment
    Eh = Lambda(lambda x: keras.activations.softmax(x))(Eph)  # [batch_size, Hsize, Psize]
    HypoAlign = keras.layers.Dot((2, 1))([Eh, embed_p])
    HypoPair = keras.layers.Concatenate()([embed_h, HypoAlign])

    # 5, BiGRU Encoder
    final = Bidirectional(LSTM(units=256, dropout=self.DropProb))(HypoPair) # [-1,2*units]
    for i in range(2):
      final = Dense(512 if i == 1 else 256,
                    activation='relu',
                    kernel_regularizer=l2(self.L2Strength),
                    bias_regularizer=l2(self.L2Strength))(final)
      final = Dropout(self.DropProb)(final)
      final = BatchNormalization()(final)

    # 6, Prediction by softmax
    final = Dense(3, activation='softmax',name='judger')(final)
    self.model = Model(inputs=[premise, hypothesis], outputs=final)

  @time_count
  def compile_model(self):
    """ Load Possible Existing Weights and Compile the Model """
    self.model.compile(optimizer=self.Optimizer,
                       loss='categorical_crossentropy',
                       metrics=['accuracy'])
    self.model.summary()
    fn = self.rnn_type + '.check'
    if os.path.exists(fn):
      self.model.load_weights(fn, by_name=True)
      print('--------Load Weights Successful!--------')

  def start_train(self):
    """ Starts to Train the entire Model Based on set Parameters """
    # 1, Prep
    #self.compile_model()
    callback = [EarlyStopping(patience=self.Patience),
                ReduceLROnPlateau(patience=6, verbose=1),
                CSVLogger(filename=self.rnn_type+'log.csv'),
                ModelCheckpoint(self.rnn_type + '.check', save_best_only=True, save_weights_only=True)]

    # 2, Train
    self.model.fit(x = [self.train[0],self.train[1]],
                   y = self.train[2],
                   batch_size = self.BatchSize,
                   epochs = self.MaxEpoch,
                   #validation_data = ([self.validation[0],self.validation[1]],self.validation[2]),
                   validation_data=([self.validation[0], self.validation[1]], self.validation[2]),
                   callbacks = callback)

    # 3, Evaluate
    self.model.load_weights(self.rnn_type + '.check') # revert to the best model
    #loss, acc = self.model.evaluate([self.test[0],self.test[1]],self.test[2],batch_size=self.BatchSize)
    loss, acc = self.model.evaluate([self.test[0],self.test[1]],
                                    self.test[2],batch_size=self.BatchSize)
    return loss, acc # loss, accuracy on test data set

  def evaluate_on_test(self):
    loss, acc = self.model.evaluate([self.test[0],self.test[1]],
                                    self.test[2],batch_size=self.BatchSize)
    print("Test: loss = {:.5f}, acc = {:.3f}%".format(loss,acc*100))

  @staticmethod
  def plotHeatMap(df, psize=(8,8), filename='Heatmap'):
    ax = sns.heatmap(df, vmax=.85, square=True, cbar=False, annot=True)
    plt.xticks(rotation=40), plt.yticks(rotation=360)
    fig = ax.get_figure()
    fig.set_size_inches(psize)
    fig.savefig(filename)
    plt.clf()


  def interactive_predict(self, test_mode = False):
    """ The model must be compiled before execuation """
    prep_alfa = lambda X: pad_sequences(sequences=self.indexer.texts_to_sequences(X),
                                        maxlen=self.SentMaxLen)
    while True:
      prem = input("Please input the premise:\n")
      hypo = input("Please input another sent:\n")
      unknown = set([word for word in list(filter(lambda x: x and x != ' ',
                                                  re.split(r'(\W)',prem) + re.split(r'(\W)',hypo)))
                          if word not in self.indexer.word_counts.keys()])
      if unknown:
        print('[WARNING] {} Unregistered Words:{}'.format(len(unknown),unknown))
      prem_pad, hypo_pad = prep_alfa([prem]), prep_alfa([hypo])
      if test_mode:
        ans = self.model.predict(x=[prem_pad,hypo_pad],batch_size=1)
        Ep, Eh = np.array(ans[0]).reshape(36,36),np.array(ans[1]).reshape(36,36) # [P,H] [H,P]
        Ep = Ep[-len(prem.split(' ')):,-len(hypo.split(' ')):] # [P,H]
        Eh = Eh[-len(hypo.split(' ')):,-len(prem.split(' ')):] # [H,P]
        self.plotHeatMap(pd.DataFrame(Ep,columns=hypo.split(' '),index=prem.split(' ')),
                         psize=(7,10), filename='Ep')
        self.plotHeatMap(pd.DataFrame(Eh,columns=prem.split(' '),index=hypo.split(' ')),
                         psize=(10,7), filename='Eh')
        ans = np.reshape(ans[2],-1)
      else:
        ans = np.reshape(self.model.predict(x=[prem,hypo],batch_size=1),-1) # PREDICTION
      print('\n Contradiction \t{:.1f}%\n'.format(float(ans[0]) * 100),
            'Neutral \t\t{:.1f}%\n'.format(float(ans[1]) * 100),
            'Entailment \t{:.1f}%\n'.format(float(ans[2]) * 100))


if __name__ == '__main__':
  md = AttentionAlignmentModel(annotation='SoftAlign2')
  md.prep_data()
  md.prep_embd()
  _test = False
  #md.create_model(test_mode = _test)
  md.create_model2()
  md.compile_model()
  md.start_train()
  #md.evaluate_on_test()
  #md.interactive_predict(test_mode = _test)

