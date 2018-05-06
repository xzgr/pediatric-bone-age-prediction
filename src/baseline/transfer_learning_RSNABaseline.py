import numpy as np
import pandas as pd  # data processing, CSV file I/O (e.g. pd.read_csv)
import matplotlib.pyplot as plt  # showing and rendering figures
# io related
from skimage.io import imread
import os
from glob import glob
from keras.preprocessing.image import ImageDataGenerator
# from keras.applications.resnet50 import preprocess_input
from keras.applications.vgg16 import preprocess_input
##split data into training and validation
from sklearn.model_selection import train_test_split
from keras.applications.resnet50 import ResNet50
from keras.applications.vgg16 import VGG16
from keras.layers import GlobalAveragePooling2D, Dense, Dropout, Flatten, Input, Conv2D, multiply, LocallyConnected2D, \
    Lambda
from keras.models import Model
from keras.layers import BatchNormalization
from keras.callbacks import ModelCheckpoint, LearningRateScheduler, EarlyStopping, ReduceLROnPlateau
from keras.metrics import mean_absolute_error
from datetime import datetime
from transfer_learning_common import flow_from_dataframe, get_chest_dataframe

tstart = datetime.now()
print('start: ', tstart)

base_bone_dir = '/var/tmp/studi5/boneage/datasets/boneage'
age_df = pd.read_csv(os.path.join(base_bone_dir, 'boneage-training-dataset.csv'))  # read csv
age_df['path'] = age_df['id'].map(lambda x: os.path.join(base_bone_dir, 'boneage-training-dataset',
                                                         '{}.png'.format(x)))  # add path to dictionary
age_df['exists'] = age_df['path'].map(os.path.exists)  # add exists to dictionary
print(age_df['exists'].sum(), 'images found of', age_df.shape[0], 'total')  # print how many images have been found
age_df['gender'] = age_df['male'].map(lambda x: 'male' if x else 'female')  # convert boolean to string male or female
boneage_mean = age_df['boneage'].mean()
boneage_div = 2 * age_df['boneage'].std()
boneage_mean = 0
boneage_div = 1.0
age_df['boneage_zscore'] = age_df['boneage'].map(lambda x: (x - boneage_mean) / boneage_div) # creates classes
age_df.dropna(inplace=True)
age_df.sample(3)
# age_df[['boneage', 'male', 'boneage_zscore']].hist(figsize=(10, 5))
age_df['boneage_category'] = pd.cut(age_df['boneage'], 10)

raw_train_df, valid_df = train_test_split(age_df, test_size=0.2, random_state=2018, stratify=age_df['boneage_category'])
print('train', raw_train_df.shape[0], 'validation', valid_df.shape[0])
# Balance the distribution in the training set
train_df = raw_train_df.groupby(['boneage_category', 'male']).apply(lambda x: x.sample(500, replace=True)
                                                                    ).reset_index(drop=True)
print('New Data Size:', train_df.shape[0], 'Old Size:', raw_train_df.shape[0])
# train_df[['boneage', 'male']].hist(figsize=(10, 5))

IMG_SIZE = (384, 384)  # slightly smaller than vgg16 normally expects
core_idg = ImageDataGenerator(samplewise_center=False,
                              samplewise_std_normalization=False,
                              horizontal_flip=True,
                              vertical_flip=False,
                              height_shift_range=0.15,
                              width_shift_range=0.15,
                              rotation_range=5,
                              shear_range=0.01,
                              fill_mode='nearest',
                              zoom_range=0.25,
                              preprocessing_function=preprocess_input)


train_gen = flow_from_dataframe(core_idg, train_df, path_col='path', y_col='boneage_zscore', target_size=IMG_SIZE,
                                color_mode='rgb', batch_size=32)

valid_gen = flow_from_dataframe(core_idg, valid_df, path_col='path', y_col='boneage_zscore', target_size=IMG_SIZE,
                                color_mode='rgb', batch_size=256)  # we can use much larger batches for evaluation

# used a fixed dataset for evaluating the algorithm
test_X, test_Y = next(
    flow_from_dataframe(core_idg, valid_df, path_col='path', y_col='boneage_zscore', target_size=IMG_SIZE,
                        color_mode='rgb', batch_size=256))  # one big batch

t_x, t_y = next(train_gen)
in_lay = Input(t_x.shape[1:])
base_pretrained_model = VGG16(input_shape=t_x.shape[1:], include_top=False, weights='imagenet')
base_pretrained_model.trainable = False
pt_depth = base_pretrained_model.get_output_shape_at(0)[-1]
pt_features = base_pretrained_model(in_lay)

bn_features = BatchNormalization()(pt_features)

# here we do an attention mechanism to turn pixels in the GAP on and off

attn_layer = Conv2D(64, kernel_size=(1, 1), padding='same', activation='relu')(bn_features)
attn_layer = Conv2D(16, kernel_size=(1, 1), padding='same', activation='relu')(attn_layer)
attn_layer = LocallyConnected2D(1,
                                kernel_size=(1, 1),
                                padding='valid',
                                activation='sigmoid')(attn_layer)
# fan it out to all of the channels
up_c2_w = np.ones((1, 1, 1, pt_depth))
up_c2 = Conv2D(pt_depth, kernel_size=(1, 1), padding='same',
               activation='linear', use_bias=False, weights=[up_c2_w])
up_c2.trainable = False
attn_layer = up_c2(attn_layer)

mask_features = multiply([attn_layer, bn_features])
gap_features = GlobalAveragePooling2D()(mask_features)
gap_mask = GlobalAveragePooling2D()(attn_layer)
# to account for missing values from the attention model
gap = Lambda(lambda x: x[0] / x[1], name='RescaleGAP')([gap_features, gap_mask])
gap_dr = Dropout(0.5)(gap)
dr_steps = Dropout(0.25)(Dense(1024, activation='elu')(gap_dr))
out_layer = Dense(1, activation='linear')(dr_steps)  # linear is what 16bit did
bone_age_model = Model(inputs=[in_lay], outputs=[out_layer])


def mae_months(in_gt, in_pred):
    return mean_absolute_error(boneage_div * in_gt, boneage_div * in_pred)


bone_age_model.compile(optimizer='adam', loss='mse', metrics=[mae_months])

bone_age_model.summary()

weight_path = base_bone_dir + "{}_weights.best.hdf5".format('bone_age')

checkpoint = ModelCheckpoint(weight_path, monitor='val_loss', verbose=1,
                             save_best_only=True, mode='min', save_weights_only=True)

reduceLROnPlat = ReduceLROnPlateau(monitor='val_loss', factor=0.8, patience=10, verbose=1, mode='auto', epsilon=0.0001,
                                   cooldown=5, min_lr=0.0001)
early = EarlyStopping(monitor="val_loss",
                      mode="min",
                      patience=5)  # probably needs to be more patient, but kaggle time is limited
callbacks_list = [checkpoint, early, reduceLROnPlat]

print('==================================================')
print('======= Training Model on CHEST Dataset ==========')
print('==================================================')
class_str_col = 'Patient Age'
chest_df = get_chest_dataframe('nih-chest-xrays/')
train_df_chest, valid_df_chest = train_test_split(chest_df, test_size=0.2, random_state=2018)  # , stratify=chest_df['chest_category'])
print('train_chest', train_df_chest.shape[0], 'validation_chest', valid_df_chest.shape[0])

train_gen_chest = flow_from_dataframe(core_idg, train_df_chest, path_col='path', y_col=class_str_col, target_size=IMG_SIZE,
                                      color_mode='rgb', batch_size=32)

valid_gen_chest = flow_from_dataframe(core_idg, valid_df_chest, path_col='path', y_col=class_str_col, target_size=IMG_SIZE,
                                      color_mode='rgb', batch_size=128)  # we can use much larger batches for evaluation

bone_age_model.fit_generator(train_gen_chest,
                             validation_data=valid_gen_chest,
                             epochs=15,
                             callbacks=callbacks_list)

print('==================================================')
print('======= Training Model on BONEAGE Dataset ========')
print('==================================================')

bone_age_model.fit_generator(train_gen,
                             validation_data=(test_X, test_Y),
                             epochs=15,
                             callbacks=callbacks_list)

tend = datetime.now()
print('elapsed time: %s' % str((tend-tstart)))
