import numpy as np
import h5py as h5
from sklearn.utils import shuffle
import sys
import os
import tensorflow as tf
import gc
import random
import itertools
import pickle, copy
from scipy.stats import norm
import horovod.tensorflow.keras as hvd

def setup_gpus():
    hvd.init()
    gpus = tf.config.experimental.list_physical_devices('GPU')
    for gpu in gpus:
        tf.config.experimental.set_memory_growth(gpu, True)
    if gpus:
        tf.config.experimental.set_visible_devices(gpus[hvd.local_rank()], 'GPU')

def load_pickle(folder,f):
    file_name = os.path.join(folder,'histories',f.replace(".weights.h5",".pkl"))
    with open(file_name, 'rb') as file_pi:
        history_dict = pickle.load(file_pi)
    return history_dict

def revert_npart(nparts):
    # Reverse the preprocessing to recover the particle multiplicity
    mean = np.array([0.275, 0.188, 32.7, 22.3, 94.7])
    std = np.array([0.542, 0.455, 12.5, 8.39, 31.4])
    return np.round(nparts * std + mean).astype(np.int32)


class DataLoader:
    """Base class for all data loaders with common preprocessing methods."""
    def __init__(self, path, names = [], correction = [], reference = [], batch_size=512, rank=0, size=1, chunk_size=5000,corrector = False,**kwargs):

        self.path = path
        self.batch_size = batch_size
        self.rank = rank
        self.size = size
        self.chunk_size = chunk_size
        self.correction = correction
        self.reference = reference

        self.corrector = corrector
        if self.corrector:
            assert len(self.correction) > 0 and len(self.reference) > 0, "ERROR: Reference and Correction not given"
        
        self.mean_part = np.array([0.0, 0.0, 0.255,
                                   0.03836162 , 0.06146408 , 0.06212494,
                                   0.0, 0.0, 0.0, 0.0, 0.0, 0.0])
        self.std_part = np.array([1.0 , 1.0 , 0.6952,
                                  0.57357428, 0.58680139, 2.94017443,
                                  1.0, 1.0, 1.0, 1.0, 1.0, 1.0 ])

        self.mean_evt =  np.array([-9.03940100e-01, -5.47091213e-01,  1.42402418e+03,
                                   0.275, 0.188, 32.7, 22.3, 94.7])
        self.std_evt  = np.array([45.25099801,  43.78186693, 181.1162556,
                                  0.542, 0.455, 12.5, 8.39, 31.4])

        self.part_names = ['$\eta$', '$\phi$', 'log($1 + p_{Trel}$)',
                           'vx', 'vy', 'vz',
                           'is electron','is muon', 'is photon',
                           'is neutral hadron','is charged hadron']

        self.evt_names = ['$MET_x$ [GeV]','$MET_y$ [GeV]','HT [GeV]',
                          'nelectron','nmuon','nphoton','nneutral','ncharged']

        # self.evt_names = ['$x$', '$Q^2$ [GeV]$^2$','$W$', '$y$', '$\nu$']

        
        if self.corrector:            
            self.reference = [os.path.join(self.path, self.reference)]
            self.correction = [os.path.join(self.path, self.correction)]
            all_files = self.reference + self.correction

        else:
            all_files = [
                os.path.join(self.path, f)
                for f in os.listdir(path)
                if os.path.isfile(os.path.join(path, f)) and any(name in f for name in names)
            ]
            self.files = np.array_split(all_files,self.size)[self.rank]

            
        self.get_stats(all_files)

    def get_stats(self,file_list):
        self.nevts = 0
        for f in file_list:
            self.nevts+= h5.File(f,'r')['reco_evt'].shape[0]
        #print(file_list)
        self.num_part = h5.File(file_list[0],'r')['reco'].shape[1]
        self.num_feat = h5.File(file_list[0],'r')['reco'].shape[2]
        self.num_evt = h5.File(file_list[0],'r')['reco_evt'].shape[1]
        self.steps_per_epoch = self.nevts//self.size//self.batch_size

        if self.rank ==0:
            print(f"Loaded dataset with {self.num_part} particles and {self.num_feat} features")

                
    def get_preprocess_cond(self,nevts=-1):
        if nevts<0:
            nevts = self.nevts
        
        self.gen =  np.concatenate([h5.File(f, 'r')['gen'][:] for f in self.files], axis=0)[:nevts]
        self.gen_evt = np.concatenate([h5.File(f, 'r')['gen_evt'][:] for f in self.files], axis=0)[:nevts]
        evtn = np.concatenate([h5.File(f, 'r')['eventNumber'][:] for f in self.files], axis=0)[:nevts]
        self.gen_mask = self.gen[:, :, 2] != 0  
            
        gen = self.preprocess(self.gen,self.gen_mask).astype(np.float32)
        gen_evt = self.preprocess_evt(self.gen_evt).astype(np.float32)
        return gen, self.gen_mask.astype(np.float32), gen_evt, evtn.astype(np.int32)

        
    def data_from_file(self,files, nevts = None,preprocess=False):
        reco_data_chunk = np.concatenate([h5.File(f, 'r')['reco'][:] for f in files], axis=0)[:nevts]
        reco_mask_chunk = reco_data_chunk[:, :, 2] != 0
        gen_data_chunk = np.concatenate([h5.File(f, 'r')['gen'][:] for f in files], axis=0)[:nevts]
        gen_mask_chunk = gen_data_chunk[:, :, 2] != 0
        gen_evt_chunk = np.concatenate([h5.File(f, 'r')['gen_evt'][:] for f in files], axis=0)[:nevts]
        reco_evt_chunk = np.concatenate([h5.File(f, 'r')['reco_evt'][:] for f in files], axis=0)[:nevts]

        if preprocess:
            reco_data_chunk = self.preprocess(reco_data_chunk, reco_mask_chunk)
            gen_data_chunk = self.preprocess(gen_data_chunk, gen_mask_chunk)
            reco_evt_chunk = self.preprocess_evt(reco_evt_chunk)
            gen_evt_chunk = self.preprocess_evt(gen_evt_chunk)
            
        return [reco_data_chunk,gen_data_chunk,
                reco_mask_chunk,gen_mask_chunk,
                reco_evt_chunk, gen_evt_chunk]


    def preprocess(self,x,mask):                
        num_feat = self.mean_part.shape[0]
        new_features = x.copy()
        new_features[:,:,:num_feat] =  mask[:,:, None]*(x[:,:,:num_feat]-self.mean_part)/self.std_part
        new_features[np.isnan(new_features)] = 0.0
        new_features[np.isinf(new_features)] = 0.0
        return new_features

    def preprocess_evt(self,x):
        new_features = (x-self.mean_evt)/self.std_evt
        new_features[np.isnan(new_features)] = 0.0
        new_features[np.isinf(new_features)] = 0.0
        
        return new_features

    def revert_preprocess(self,x,mask):                
        num_feat = self.mean_part.shape[-1]        
        new_part = mask[:,:, None]*(x[:,:,:num_feat]*self.std_part + self.mean_part)
        #charge
        new_part[:,:,6] = np.sign(new_part[:,:,6])
        #pids
        max_indices = np.argmax(new_part[:,:,7:], axis=-1)
        pids = np.zeros_like(new_part[:,:,7:])
        pids[np.arange(new_part.shape[0])[:, None], np.arange(new_part.shape[1]), max_indices] = 1
        new_part[:,:,7:] = pids
        #zero vertex and pids for neutral particles
        neutral_mask = (pids[:,:,2]==1) | (pids[:,:,3]==1)
        new_part[:,:,3:7] *= (1.0-neutral_mask[:,:,None])
                
        return  new_part

    def revert_preprocess_evt(self,x):
        new_x = self.std_evt*x+self.mean_evt
        #Convert multiplicity back into integers
        new_x[:,3:] = np.round(new_x[:,3:])
        new_x[:,3:] = np.clip(new_x[:,3:],0,self.num_part)
        return new_x



    def single_file_generator(self, file_path):
        with h5.File(file_path, 'r') as file:
            data_size = file['reco_evt'].shape[0]
            for start in range(0, data_size, self.chunk_size):
                end = min(start + self.chunk_size, data_size)
                
                reco_chunk = file['reco'][start:end].astype(np.float32)
                gen_chunk = file['gen'][start:end].astype(np.float32)
                reco_evt_chunk = file['reco_evt'][start:end]
                gen_evt_chunk = file['gen_evt'][start:end]
                reco_mask_chunk = reco_chunk[:, :, 2] != 0
                gen_mask_chunk = gen_chunk[:, :, 2] != 0  
                
                reco_chunk = self.preprocess(reco_chunk, reco_mask_chunk).astype(np.float32)
                gen_chunk = self.preprocess(gen_chunk, gen_mask_chunk).astype(np.float32)
                
                reco_evt_chunk = self.preprocess_evt(reco_evt_chunk).astype(np.float32)
                gen_evt_chunk = self.preprocess_evt(gen_evt_chunk).astype(np.float32)

                for j in range(reco_chunk.shape[0]):                        
                    yield {
                        'input_reco': reco_chunk[j],
                        'input_gen': gen_chunk[j],
                        'input_reco_mask': reco_mask_chunk[j],
                        'input_gen_mask': gen_mask_chunk[j],
                        'input_reco_evt': reco_evt_chunk[j],
                        'input_gen_evt': gen_evt_chunk[j]}
                
    def interleaved_file_generator(self):
        random.shuffle(self.files)
        generators = [self.single_file_generator(fp) for fp in self.files]
        round_robin_generators = itertools.cycle(generators)

        while True:
            try:
                next_gen = next(round_robin_generators)
                yield next(next_gen)
            except StopIteration:
                break

    def make_tfdata(self):

        if self.corrector:
            reco_data,gen_data,reco_mask,gen_mask,_, _ = self.data_from_file(self.correction, preprocess=True)
            label = self.data_from_file(self.reference, preprocess=True)[0]
            dataset = tf.data.Dataset.from_tensor_slices(
                {'input_reco':reco_data.astype(np.float32),
                 'input_gen':gen_data.astype(np.float32),
                 'input_reco_mask':reco_mask.astype(np.float32),
                 'input_gen_mask':gen_mask.astype(np.float32),
                 'input_label':label[:reco_data.shape[0]].astype(np.float32),
                 })
                    
        else:
            dataset = tf.data.Dataset.from_generator(
                self.interleaved_file_generator,
                output_signature=(
                    {'input_reco': tf.TensorSpec(shape=(self.num_part, self.num_feat), dtype=tf.float32),
                     'input_gen': tf.TensorSpec(shape=(self.num_part, self.num_feat), dtype=tf.float32),                 
                     'input_reco_mask': tf.TensorSpec(shape=(self.num_part), dtype=tf.float32),
                     'input_gen_mask': tf.TensorSpec(shape=(self.num_part), dtype=tf.float32),
                     'input_reco_evt': tf.TensorSpec(shape=(self.num_evt), dtype=tf.float32),
                     'input_gen_evt': tf.TensorSpec(shape=(self.num_evt), dtype=tf.float32)}
                ))
        
        return dataset.shuffle(self.batch_size*50).repeat().batch(self.batch_size).prefetch(tf.data.AUTOTUNE)
        
