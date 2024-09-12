import tensorflow as tf
from tensorflow import keras
from tensorflow.keras.models import Model
from tensorflow.keras import layers
from layers import StochasticDepth, TalkingHeadAttention, LayerScale, RandomDrop
from tensorflow.keras.losses import mse, categorical_crossentropy
import numpy as np
from tqdm import tqdm
import utils

class PET(keras.Model):
    """Point-Edge Transformer"""
    def __init__(self,
                 num_feat,
                 num_evt,
                 num_part,
                 projection_dim = 96,
                 K = 5,
                 num_layers = 8,
                 num_local = 2,
                 num_heads = 4,
                 layer_scale_init = 1e-5,
                 num_add_gen = 0 #Number of additional PID features gen only
                 ):

        super(PET, self).__init__()
        self.num_feat = num_feat
        self.num_evt = num_evt
        self.max_part = num_part
        self.num_heads = num_heads
        self.num_layers = num_layers
        self.projection_dim = projection_dim
        self.layer_scale_init=layer_scale_init
        self.ema=0.999
        self.num_steps = 128
        self.shape = (-1,1,1)
        self.num_add_gen = num_add_gen
        self.num_local = num_local
        self.K = K
        
        input_reco = layers.Input(shape=(None, num_feat), name='input_reco')
        input_gen = layers.Input(shape=(None, num_feat+num_add_gen), name='input_gen')
        
        input_reco_mask = layers.Input((None,1),name = 'input_reco_mask')
        input_gen_mask = layers.Input((None,1),name = 'input_gen_mask')
        
        input_reco_evt = layers.Input((num_evt),name='input_reco_evt')
        input_gen_evt = layers.Input((num_evt),name='input_gen_evt')
        
        input_time = layers.Input((1),name = 'input_time')


        outputs_body = self.PET_body(input_reco,
                                     input_gen,
                                     input_reco_mask,
                                     input_gen_mask,
                                     input_reco_evt,
                                     input_gen_evt,
                                     input_time,
                                     K = self.K)

        self.body = keras.Model(inputs=[input_reco,input_gen,
                                        input_reco_mask,input_gen_mask,
                                        input_reco_evt,input_gen_evt,
                                        input_time],
                                outputs=outputs_body)
                
        outputs_generator = self.PET_generator(outputs_body,
                                               input_reco_mask,
                                               # input_gen,
                                               # input_gen_mask,
                                               K = self.K
                                               )

        self.generator_head = keras.Model(inputs=[outputs_body,
                                                  input_reco_mask,
                                                  # input_gen,
                                                  # input_gen_mask,
                                                  ],
                                          outputs=outputs_generator)
        
        self.generator = keras.Model(inputs=[input_reco,input_gen,
                                             input_reco_mask,input_gen_mask,
                                             input_reco_evt,input_gen_evt,
                                             input_time],
                                     outputs=outputs_generator)


        outputs = self.Resnet(
            input_reco_evt,
            input_gen_evt,
            input_gen,
            input_gen_mask,
            input_time,
            num_layer = 3,
            mlp_dim= self.projection_dim,
        )

        

        self.model_evt = Model(inputs=[input_reco_evt,
                                       input_gen_evt,
                                       input_gen,
                                       input_gen_mask,                                       
                                       input_time],
                               outputs=outputs)


        self.ema_evt = keras.models.clone_model(self.model_evt)
        self.ema_body = keras.models.clone_model(self.body)
        self.ema_head = keras.models.clone_model(self.generator_head)

        
        self.loss_tracker = keras.metrics.Mean(name="loss")
        self.loss_part_tracker = keras.metrics.Mean(name="part")
        self.loss_evt_tracker = keras.metrics.Mean(name="evt")
        
    @property
    def metrics(self):
        """List of the model's metrics.
        We make sure the loss tracker is listed as part of `model.metrics`
        so that `fit()` and `evaluate()` are able to `reset()` the loss tracker
        at the start of each epoch and at the start of an `evaluate()` call.
        """
        return [self.loss_tracker,self.loss_part_tracker,self.loss_evt_tracker]


    def call(self,x):
        return self.generator(x)

    def train_step(self, inputs):        
        batch_size = tf.shape(inputs['input_reco_evt'])[0]

        with tf.GradientTape(persistent=True) as tape:            
            t = tf.random.uniform((batch_size,1))                
            logsnr, alpha, sigma = get_logsnr_alpha_sigma(t)
            
            eps = tf.random.normal((tf.shape(inputs['input_reco'])),
                                   dtype=tf.float32)*inputs['input_reco_mask'][:,:,None]
                            
            perturbed_x = alpha[:,None]*inputs['input_reco'] + eps * sigma[:,None]
                        
            v_pred_part = self.generator([perturbed_x,
                                          inputs['input_gen'],
                                          inputs['input_reco_mask'],inputs['input_gen_mask'],
                                          inputs['input_reco_evt'],inputs['input_gen_evt'],t])
            
            v_pred_part = tf.reshape(v_pred_part,(tf.shape(v_pred_part)[0], -1))
            v_part = alpha[:,None] * eps - sigma[:,None] * inputs['input_reco']
            v_part = tf.reshape(v_part,(tf.shape(v_part)[0], -1))

            #loss_part = tf.reduce_mean(tf.square(v_part-v_pred_part))
            loss_part = tf.reduce_sum(tf.square(v_part-v_pred_part))/(tf.reduce_sum(inputs['input_reco_mask']))

        
            #Event model

            eps = tf.random.normal((batch_size,self.num_evt),dtype=tf.float32)
            perturbed_x = alpha*inputs['input_reco_evt'] + eps * sigma            
            v_pred = self.model_evt([perturbed_x,
                                     inputs['input_gen_evt'],
                                     inputs['input_gen'],
                                     inputs['input_gen_mask'],
                                     t])
            
            v_evt = alpha * eps - sigma * inputs['input_reco_evt']
            loss_evt = tf.reduce_mean(tf.square(v_pred-v_evt))

            loss = loss_evt + loss_part


        self.body_optimizer.minimize(loss_part,self.body.trainable_variables,tape=tape)
                   
        trainable_vars = self.model_evt.trainable_variables + self.generator_head.trainable_variables
        self.optimizer.minimize(loss,trainable_vars,tape=tape)

        
        self.loss_tracker.update_state(loss)
        self.loss_part_tracker.update_state(loss_part)
        self.loss_evt_tracker.update_state(loss_evt)

            
        for weight, ema_weight in zip(self.model_evt.weights, self.ema_evt.weights):
            ema_weight.assign(self.ema * ema_weight + (1 - self.ema) * weight)
            
        for weight, ema_weight in zip(self.generator_head.weights, self.ema_head.weights):
            ema_weight.assign(self.ema * ema_weight + (1 - self.ema) * weight)

        for weight, ema_weight in zip(self.body.weights, self.ema_body.weights):
            ema_weight.assign(self.ema * ema_weight + (1 - self.ema) * weight)


        return {m.name: m.result() for m in self.metrics}

    
    def test_step(self, inputs):
        batch_size = tf.shape(inputs['input_reco_evt'])[0]


        t = tf.random.uniform((batch_size,1))                
        logsnr, alpha, sigma = get_logsnr_alpha_sigma(t)
        
        eps = tf.random.normal((tf.shape(inputs['input_reco'])),
                               dtype=tf.float32)*inputs['input_reco_mask'][:,:,None]
        
        perturbed_x = alpha[:,None]*inputs['input_reco'] + eps * sigma[:,None]
        
        v_pred_part = self.generator([perturbed_x,
                                      inputs['input_gen'],
                                      inputs['input_reco_mask'],inputs['input_gen_mask'],
                                      inputs['input_reco_evt'],inputs['input_gen_evt'],t])
        
        v_pred_part = tf.reshape(v_pred_part,(tf.shape(v_pred_part)[0], -1))
        v_part = alpha[:,None] * eps - sigma[:,None] * inputs['input_reco']
        v_part = tf.reshape(v_part,(tf.shape(v_part)[0], -1))


        loss_part = tf.reduce_sum(tf.square(v_part-v_pred_part))/(tf.reduce_sum(inputs['input_reco_mask']))
        
        
        #Event model
        
        eps = tf.random.normal((batch_size,self.num_evt),dtype=tf.float32)
        perturbed_x = alpha*inputs['input_reco_evt'] + eps * sigma            
        v_pred = self.model_evt([perturbed_x,
                                 inputs['input_gen_evt'],
                                 inputs['input_gen'],
                                 inputs['input_gen_mask'],
                                 t])
            
        v_evt = alpha * eps - sigma * inputs['input_reco_evt']
        loss_evt = tf.reduce_mean(tf.square(v_pred-v_evt))
        
        loss = loss_evt + loss_part
        self.loss_tracker.update_state(loss)
        self.loss_part_tracker.update_state(loss_part)
        self.loss_evt_tracker.update_state(loss_evt)

        return {m.name: m.result() for m in self.metrics}



    def Resnet(self,
               input_reco_evt,
               input_gen_evt,
               input_gen,
               input_gen_mask,
               input_time,
               num_layer = 3,
               mlp_dim=128,
               dropout=0.0,
               ):
    
        def resnet_dense(input_layer,hidden_size,nlayers=2):
            x = input_layer
            residual = layers.Dense(hidden_size)(x)
            for _ in range(nlayers):
                x = layers.Dense(hidden_size,activation='swish')(x)
                x = layers.Dropout(dropout)(x)
            x = LayerScale(self.layer_scale_init,hidden_size)(x)
            return residual + x

        time = FourierProjection(input_time,self.projection_dim)
        
        gen_embedding = get_encoding(input_gen,self.projection_dim)*input_gen_mask
        gen_embedding = tf.reduce_mean(gen_embedding,1)        
        gen_evt_embedding = layers.Dense(self.projection_dim)(input_gen_evt)
        conditional = tf.concat([time,gen_evt_embedding,gen_embedding],-1)
        cond_token = layers.Dense(2*self.projection_dim,activation='gelu')(conditional)
        scale,shift = tf.split(cond_token,2,-1)
        
        layer = layers.Dense(self.projection_dim,activation='swish')(input_reco_evt)
        layer = layer*(1.0+scale) + shift
        
        for _ in range(num_layer-1):
            layer = layers.LayerNormalization(epsilon=1e-6)(layer)
            layer =  resnet_dense(layer,mlp_dim)

        layer = layers.LayerNormalization(epsilon=1e-6)(layer)
        outputs = layers.Dense(self.num_evt,kernel_initializer="zeros")(layer)
    
        return outputs


                
    def PET_body(self,
                 input_reco,
                 input_gen,
                 input_reco_mask,
                 input_gen_mask,
                 input_reco_evt,
                 input_gen_evt,
                 input_time,
                 K,
                 ):

        encoded = get_encoding(input_reco,self.projection_dim)
        gen_encoded = get_encoding(input_gen,self.projection_dim)
        gen_encoded = layers.GroupNormalization(groups=1)(gen_encoded)*input_gen_mask
        

        #Event and time Conditional info
        time = FourierProjection(input_time,self.projection_dim)
        cond_gen = get_encoding(input_gen_evt,self.projection_dim)
        time = tf.concat([time,cond_gen],-1)
        time = tf.tile(time[:,None, :], [1,tf.shape(encoded)[1], 1])
        time = layers.Dense(2*self.projection_dim,activation='gelu')(time)*input_reco_mask
        scale,shift = tf.split(time,2,-1)        
        encoded = encoded*(1.0+scale) + shift

        
        #Local info
        
        coord_shift_reco = tf.multiply(999., tf.cast(tf.equal(input_reco_mask, 0), dtype='float32'))
        coord_shift_gen = tf.multiply(999., tf.cast(tf.equal(input_gen_mask, 0), dtype='float32'))
        points_reco = input_reco
        points_gen = input_gen
        local_features = input_reco
        local_gens = input_gen
        
        for _ in range(self.num_local):    
            local_features = get_neighbors(coord_shift_reco + points_reco,
                                           #tf.concat([coord_shift_gen + points_gen,coord_shift_reco + points_reco],1),
                                           coord_shift_gen + points_gen,
                                           local_features,
                                           local_gens,
                                           #tf.concat([local_gens,local_features],1),
                                           self.projection_dim,K)
            local_gens = layers.Dense(self.projection_dim,activation="gelu")(local_gens)
            points_reco = local_features
            points_gen = local_gens
            
            
        encoded = layers.Add()([local_features,encoded])*input_reco_mask
        #encoded = layers.Concatenate(-1)([local_features,encoded])
        #encoded = layers.Dense(self.projection_dim)(encoded)*input_reco_mask

        skip_connection = encoded
        for i in range(self.num_layers):
            x1 = layers.GroupNormalization(groups=1)(encoded)

            updates = layers.MultiHeadAttention(num_heads=self.num_heads,
                                                key_dim=self.projection_dim//self.num_heads)(x1,x1,
                                                attention_mask = tf.cast(tf.matmul(input_reco_mask,input_reco_mask,transpose_b=True),tf.bool))
            
            updates = layers.GroupNormalization(groups=1)(updates)
            updates = LayerScale(self.layer_scale_init, self.projection_dim)(updates,input_reco_mask)
                        
            x2 = layers.Add()([updates,encoded])
            x3 = layers.GroupNormalization(groups=1)(x2)
            x3 = layers.Dense(4*self.projection_dim,activation="gelu")(x3)
            x3 = layers.Dense(self.projection_dim)(x3)
            x3 = LayerScale(self.layer_scale_init, self.projection_dim)(x3,input_reco_mask)

            encoded = layers.Add()([x3,x2])*input_reco_mask
                       
        return encoded + skip_connection


    def compile(self,body_optimizer,head_optimizer):
        super(PET, self).compile(experimental_run_tf_function=False,
                                  weighted_metrics=[],
                                  #run_eagerly=True
                                  )
        self.body_optimizer = body_optimizer
        self.optimizer = head_optimizer


    def PET_generator(
            self,
            encoded,
            input_reco_mask,
            # input_gen,
            # input_gen_mask,
            K = 5,
    ):


        # coord_shift_reco = tf.multiply(999., tf.cast(tf.equal(input_reco_mask, 0), dtype='float32'))
        # coord_shift_gen = tf.multiply(999., tf.cast(tf.equal(input_gen_mask, 0), dtype='float32'))

        # gen_embedding = get_encoding(input_gen,self.projection_dim)*input_gen_mask
        # gen_embedding = tf.reduce_mean(gen_embedding,1)
        # encoded = encoded + gen_embedding[:,None]

        
        # encoded = get_neighbors(coord_shift_reco + encoded,
        #                         coord_shift_gen + gen_embedding,
        #                         encoded,gen_embedding,
        #                         self.projection_dim,K)

        encoded = layers.Dense(self.num_feat)(encoded)*input_reco_mask
        return encoded



    def generate(self,gen_part,gen_mask,gen_evt,nsplit = 2,use_tqdm=False):
        evt_info = []
        part_info = []

        part_splits = np.array_split(gen_part,nsplit)
        mask_part_splits = np.array_split(gen_mask,nsplit)
        evt_splits = np.array_split(gen_evt,nsplit)
        
        for split in tqdm(range(nsplit), total=nsplit, desc='Processing Splits') if use_tqdm else range(nsplit):
            evt = self.DDPMSampler(part_splits[split],
                                   mask_part_splits[split],
                                   evt_splits[split],
                                   self.ema_evt,
                                   data_shape=[part_splits[split].shape[0],self.num_evt],
                                   num_steps = 512,
                                   const_shape = [-1,1]).numpy()

            evt_info.append(evt)
            
            nparts = np.expand_dims(np.clip(utils.revert_npart(evt[:,0],name=str(self.max_part)),
                                            1,self.max_part),-1) #5 is the minimum in the datasets used for training

            mask = np.expand_dims(
                np.tile(np.arange(self.max_part),(nparts.shape[0],1)) < np.tile(nparts,(1,self.max_part)),-1)
        
            assert np.sum(np.sum(mask.reshape(mask.shape[0],-1),-1,keepdims=True)-nparts)==0, 'ERROR: Particle mask does not match the expected number of particles'

            parts = self.DDPMSampler(part_splits[split],
                                     mask_part_splits[split],
                                     evt_splits[split],
                                     [self.ema_body,self.ema_head],
                                     data_shape=[part_splits[split].shape[0],
                                                 self.max_part,self.num_feat],
                                     evt=evt,
                                     num_steps = self.num_steps,
                                     const_shape = self.shape,
                                     mask=mask.astype(np.float32)).numpy()
            part_info.append(parts*mask)            
        return np.concatenate(part_info),np.concatenate(evt_info)

    def evaluate_models(self,head,body,x,
                        cond_gen,mask_reco,mask_gen,
                        cond_reco_evt,cond_gen_evt, t):
        v = body([x,cond_gen,mask_reco,mask_gen,
                  cond_reco_evt,cond_gen_evt,t], training=False)
        v = head([v,mask_reco],training=False)
        return mask_reco*v

    @tf.function
    def second_order_correction(self,time_step,x,
                                pred_images,pred_noises,
                                alphas,sigmas, logsnr,
                                cond_evt,cond_part,mask_cond,
                                model,
                                evt=None,
                                mask=None,
                                num_steps=100,
                                second_order_alpha=0.5,shape=None):
        step_size = 1.0/num_steps
        t = time_step - second_order_alpha * step_size
        _, signal_rates, noise_rates = get_logsnr_alpha_sigma(t,shape=shape)
        noisy_images = signal_rates * pred_images + noise_rates * pred_noises

        if evt is None:
            v = model([noisy_images, cond_evt,cond_part,mask_cond,t],training=False)
        else:
            noisy_images = noisy_images*mask
            model_body, model_head = model
            v = self.evaluate_models(model_head,model_body,
                                     noisy_images,cond_part,
                                     mask,mask_cond,
                                     evt,cond_evt,t)
            
        pred_noises = noise_rates * noisy_images + signal_rates * v
        # linearly combine the two noise estimates
        pred_noises = (1.0 - 1.0 / (2.0 * second_order_alpha)) * pred_noises + 1.0 / (
            2.0 * second_order_alpha
        ) * pred_noises

        mean = tf.sqrt(1. + tf.exp(-logsnr)) * (
            x - pred_noises * tf.math.rsqrt(1. + tf.exp(logsnr)))
        
        #mean = (x - sigmas * pred_noises) / alphas        
        eps = pred_noises
        
        return mean,eps
    

    @tf.function
    def DDPMSampler(self,
                    cond_part, mask_cond,cond_evt,                    
                    model,
                    data_shape=None,
                    const_shape=None,
                    evt=None,
                    num_steps = 100,
                    mask=None):
        """Generate samples from score-based models with DDPM method.
        
        Args:
        cond: Conditional input
        model: Trained score model to use
        data_shape: Format of the data
        const_shape: Format for constants, should match the data_shape in dimensions
        evt: input evt conditional information if used
        mask: particle mask if used

        Returns: 
        Samples.
        """

        batch_size = cond_evt.shape[0]
        x = tf.random.normal(data_shape,dtype=tf.float32)

        for time_step in tf.range(num_steps, 0, delta=-1):
            t = tf.ones((batch_size, 1), dtype=tf.int32) * time_step / num_steps
            logsnr, alpha, sigma = get_logsnr_alpha_sigma(t,shape=const_shape)
            logsnr_, alpha_, sigma_ = get_logsnr_alpha_sigma(tf.ones((batch_size, 1), dtype=tf.int32) * (time_step - 1) / num_steps,shape=const_shape)
            s = inv_logsnr_schedule_cosine(0.5*(logsnr + logsnr_))
            logsnr_s, alpha_s, sigma_s = get_logsnr_alpha_sigma(s,shape=const_shape)

            
            if evt is None:
                v = model([x,cond_evt,cond_part,mask_cond,t], training=False) 
            else:
                x = x*mask
                model_body, model_head = model
                v = self.evaluate_models(model_head,model_body,
                                         x,cond_part,
                                         mask,mask_cond,
                                         evt,cond_evt,t
                                         )
            eps = v * alpha + x * sigma
            u = alpha_s/alpha* x - sigma_s*tf.math.expm1(0.25*(logsnr_ - logsnr))*eps


            if evt is None:
                v = model([u,cond_evt,cond_part,mask_cond,s], training=False) 
            else:
                u = u*mask
                model_body, model_head = model
                v = self.evaluate_models(model_head,model_body,
                                         u,cond_part,
                                         mask,mask_cond,
                                         evt,cond_evt,s
                                         )

                
            eps = v * alpha_s + u * sigma_s            
            mean = alpha_s * u - sigma_s * v
            
            x = alpha_ * mean + sigma_ * eps

        return mean



def get_neighbors(points_reco,points_gen,
                  features_reco,features_gen,
                  projection_dim,K):
    drij = pairwise_distance(points_reco,points_gen)  # (N, P, P)
    _, indices = tf.nn.top_k(-drij, k=K)  # (N, P, K+1)
    #indices = indices[:, :, 1:]  # (N, P, K)
        
    knn_fts = knn(tf.shape(points_reco)[1], K, indices, features_gen)  # (N, P, K, C)
    knn_fts_center = tf.broadcast_to(tf.expand_dims(features_reco, 2), tf.shape(knn_fts))
    local = tf.concat([knn_fts-knn_fts_center,knn_fts_center],-1)
    local = layers.Dense(projection_dim,activation='gelu')(local)
    local = tf.reduce_max(local,-2)
    
    return local


def pairwise_distance(point_cloud1,point_cloud2):
    r_A = tf.reduce_sum(point_cloud1 * point_cloud1, axis=2, keepdims=True)
    r_B = tf.reduce_sum(point_cloud2 * point_cloud2, axis=2, keepdims=True) 
    m = tf.matmul(point_cloud1, point_cloud2, transpose_b = True)
    D = r_A - 2 * m + tf.transpose(r_B, perm=(0, 2, 1)) + 1e-5
    return D


def knn(num_points, k, topk_indices, features):
    # topk_indices: (N, P, K)
    # features: (N, P, C)    
    batch_size = tf.shape(features)[0]

    batch_indices = tf.reshape(tf.range(batch_size), (-1, 1, 1))
    batch_indices = tf.tile(batch_indices, (1, num_points, k))
    indices = tf.stack([batch_indices, topk_indices], axis=-1)
    return tf.gather_nd(features, indices)


def get_encoding(x,projection_dim,use_bias=True):
    x = layers.Dense(2*projection_dim,use_bias=use_bias,activation='gelu')(x)
    x = layers.Dense(projection_dim,use_bias=use_bias,activation='gelu')(x)
    return x

def FourierProjection(x,projection_dim,num_embed=64,max_freq = 10000.):    
    half_dim = num_embed // 2
    emb = tf.math.log(max_freq) / (half_dim - 1)
    emb = tf.cast(emb,tf.float32)
    freq = tf.exp(-emb* tf.range(start=0, limit=half_dim, dtype=tf.float32))


    angle = x*freq*max_freq
    embedding = tf.concat([tf.math.sin(angle),tf.math.cos(angle)],-1)
    embedding = layers.Dense(projection_dim,activation="gelu")(embedding)
    
    return embedding


def logsnr_schedule_cosine(t, logsnr_min=-20., logsnr_max=20.):
    b = tf.math.atan(tf.exp(-0.5 * logsnr_max))
    a = tf.math.atan(tf.exp(-0.5 * logsnr_min)) - b
    return -2. * tf.math.log(tf.math.tan(a * tf.cast(t,tf.float32) + b))
    
def get_logsnr_alpha_sigma(time,shape=None):
    logsnr = logsnr_schedule_cosine(time)
    alpha = tf.sqrt(tf.math.sigmoid(logsnr))
    sigma = tf.sqrt(tf.math.sigmoid(-logsnr))
    
    if shape is not None:
        alpha = tf.reshape(alpha, shape)
        sigma = tf.reshape(sigma, shape)
        logsnr = tf.reshape(logsnr,shape)
        
    return logsnr, tf.cast(alpha,tf.float32), tf.cast(sigma,tf.float32)

@tf.function
def inv_logsnr_schedule_cosine(logsnr, logsnr_min=-20., logsnr_max=20.):
    b = tf.math.atan(tf.exp(-0.5 * logsnr_max))
    a = tf.math.atan(tf.exp(-0.5 * logsnr_min)) - b
    return tf.math.atan(tf.exp(-0.5 * tf.cast(logsnr,tf.float32)))/a -b/a
