from shapely.geometry import Polygon
import numpy as np
import cv2
from PIL import Image, ImageDraw
import math
import os
import torch
import torchvision.transforms as transforms
from torch.utils import data
from model import EAST
import lanms
import pytesseract
from matplotlib import pyplot as plt
import argparse
import sys
import pandas as pd
import string
import re

# ap = argparse.ArgumentParser()
# ap.add_argument("-i", "--image", type=str,
#     help="path to input image")

# args = ap.parse_args()

# print(args.image)

def get_rotate(theta):
    '''positive theta value means rotate clockwise'''
    return np.array([[math.cos(theta), -math.sin(theta)], [math.sin(theta), math.cos(theta)]])

def resize_img(img):
    '''resize image to be divisible by 32
    '''
    w, h = img.size
    resize_w = w
    resize_h = h

    resize_h = resize_h if resize_h % 32 == 0 else int(resize_h / 32) * 32
    resize_w = resize_w if resize_w % 32 == 0 else int(resize_w / 32) * 32
    img = img.resize((resize_w, resize_h), Image.BILINEAR)
    ratio_h = resize_h / h
    ratio_w = resize_w / w

    return img, ratio_h, ratio_w

def load_pil(img):
    '''convert PIL Image to torch.Tensor
    '''
    t = transforms.Compose([transforms.ToTensor(), transforms.Normalize(mean=(0.5,0.5,0.5),std=(0.5,0.5,0.5))])
    return t(img).unsqueeze(0)

def is_valid_poly(res, score_shape, scale):
    '''check if the poly in image scope
    Input:
        res        : restored poly in original image
        score_shape: score map shape
        scale      : feature map -> image
    Output:
        True if valid
    '''
    cnt = 0
    for i in range(res.shape[1]):
        if res[0,i] < 0 or res[0,i] >= score_shape[1] * scale or \
           res[1,i] < 0 or res[1,i] >= score_shape[0] * scale:
            cnt += 1
    return True if cnt <= 1 else False

def restore_polys(valid_pos, valid_geo, score_shape, scale=4):
    '''restore polys from feature maps in given positions
    Input:
        valid_pos  : potential text positions <numpy.ndarray, (n,2)>
        valid_geo  : geometry in valid_pos <numpy.ndarray, (5,n)>
        score_shape: shape of score map
        scale      : image / feature map
    Output:
        restored polys <numpy.ndarray, (n,8)>, index
    '''
    polys = []
    index = []
    valid_pos *= scale
    d = valid_geo[:4, :] # 4 x N
    angle = valid_geo[4, :] # N,

    for i in range(valid_pos.shape[0]):
        x = valid_pos[i, 0]
        y = valid_pos[i, 1]
        y_min = y - d[0, i]
        y_max = y + d[1, i]
        x_min = x - d[2, i]
        x_max = x + d[3, i]
        rotate_mat = get_rotate(-angle[i])

        temp_x = np.array([[x_min, x_max, x_max, x_min]]) - x
        temp_y = np.array([[y_min, y_min, y_max, y_max]]) - y
        coordidates = np.concatenate((temp_x, temp_y), axis=0)
        res = np.dot(rotate_mat, coordidates)
        res[0,:] += x
        res[1,:] += y

        if is_valid_poly(res, score_shape, scale):
            index.append(i)
            polys.append([res[0,0], res[1,0], res[0,1], res[1,1], res[0,2], res[1,2],res[0,3], res[1,3]])
    return np.array(polys), index


def get_boxes(score, geo, score_thresh=0.9, nms_thresh=0.2):
    '''get boxes from feature map
    Input:
        score       : score map from model <numpy.ndarray, (1,row,col)>
        geo         : geo map from model <numpy.ndarray, (5,row,col)>
        score_thresh: threshold to segment score map
        nms_thresh  : threshold in nms
    Output:
        boxes       : final polys <numpy.ndarray, (n,9)>
    '''
    score = score[0,:,:]
    xy_text = np.argwhere(score > score_thresh) # n x 2, format is [r, c]
    if xy_text.size == 0:
        return None

    xy_text = xy_text[np.argsort(xy_text[:, 0])]
    valid_pos = xy_text[:, ::-1].copy() # n x 2, [x, y]
    valid_geo = geo[:, xy_text[:, 0], xy_text[:, 1]] # 5 x n
    polys_restored, index = restore_polys(valid_pos, valid_geo, score.shape)
    if polys_restored.size == 0:
        return None

    boxes = np.zeros((polys_restored.shape[0], 9), dtype=np.float32)
    boxes[:, :8] = polys_restored
    boxes[:, 8] = score[xy_text[index, 0], xy_text[index, 1]]
    boxes = lanms.merge_quadrangle_n9(boxes.astype('float32'), nms_thresh)
    return boxes

def adjust_ratio(boxes, ratio_w, ratio_h):
    '''refine boxes
    Input:
        boxes  : detected polys <numpy.ndarray, (n,9)>
        ratio_w: ratio of width
        ratio_h: ratio of height
    Output:
        refined boxes
    '''
    if boxes is None or boxes.size == 0:
        return None
    boxes[:,[0,2,4,6]] /= ratio_w
    boxes[:,[1,3,5,7]] /= ratio_h
    return np.around(boxes)


def detect(img, model, device):
    '''detect text regions of img using model
    Input:
        img   : PIL Image
        model : detection model
        device: gpu if gpu is available
    Output:
        detected polys
    '''
    img, ratio_h, ratio_w = resize_img(img)
    with torch.no_grad():
        score, geo = model(load_pil(img).to(device))
    boxes = get_boxes(score.squeeze(0).cpu().numpy(), geo.squeeze(0).cpu().numpy())
    return adjust_ratio(boxes, ratio_w, ratio_h)


def plot_boxes(img, boxes):
    '''plot boxes on image
    '''
    if boxes is None:
        return img

    draw = ImageDraw.Draw(img)
    for box in boxes:
        draw.polygon([box[0], box[1], box[2], box[3], box[4], box[5], box[6], box[7]], outline=(0,255,0))
    return img



def detect_dataset(model, device, test_img_path, submit_path):
    '''detection on whole dataset, save .txt results in submit_path
    Input:
        model        : detection model
        device       : gpu if gpu is available
        test_img_path: dataset path
        submit_path  : submit result for evaluation
    '''
    img_files = os.listdir(test_img_path)
    img_files = sorted([os.path.join(test_img_path, img_file) for img_file in img_files])

    for i, img_file in enumerate(img_files):
        print('evaluating {} image'.format(i), end='\r')
        boxes = detect(Image.open(img_file), model, device)
        seq = []
        if boxes is not None:
            seq.extend([','.join([str(int(b)) for b in box[:-1]]) + '\n' for box in boxes])
        with open(os.path.join(submit_path, 'res_' + os.path.basename(img_file).replace('.jpg','.txt')), 'w') as f:
            f.writelines(seq)

model_path = './pths/east_vgg16.pth'
res_img = './res.bmp'
device = torch.device("cuda:0" if torch.cuda.is_available() else "cpu")
model = EAST().to(device)
model.load_state_dict(torch.load(model_path))
model.eval()



cnt = 0

def check(list):
    return all(i == list[0] for i in list)

image_array ,text_array =[],[]

for subdir, dirs, files in os.walk(r'/home/ubuntu/EAST/images'):
    for filename in files:
        cnt = cnt + 1

        print(filename, cnt)
        print('image_array length: ', len(image_array), 'text_array length: ', len(text_array))

        image_array.append(filename)

        img_path = './images/' + filename
        img = Image.open(img_path)
        boxes = detect(img, model, device)
        orig = cv2.imread(img_path)

        text_str = []

        if boxes is None:
            text_array.append('')
            continue
        else:
            for box in boxes:
                box = box[:-1]
                poly = [(box[0], box[1]),(box[2], box[3]),(box[4], box[5]),(box[6], box[7])]
                x = []
                y = []

                for coord in poly:
                    x.append(coord[0])
                    y.append(coord[1])

                #add a 1px buffer to prevent too close cropping

                h= orig.shape[0]
                w= orig.shape[1]

                startX = int(min(x))-1
                startY = int(min(y))-1
                endX = int(max(x))+1
                endY = int(max(y))+1


                #skip if bbox is out of image bounds
                if startY < 0 or endY < 0 or startX < 0 or endX < 0 or startY > h or endY > h or startX > w or endX > w:
                    continue

                print(startY, endY, startX, endX)
                cropped_image = orig[startY:endY, startX:endX]


            #    clahe = cv2.createCLAHE(clipLimit=4.0, tileGridSize=(5,5))
            #    res = clahe.apply(cropped_image)

            #     blur = cv2.GaussianBlur(cropped_image,(5,5),0)
            #     ret3,th3= cv2.threshold(cropped_image,0,255,cv2.THRESH_BINARY+cv2.THRESH_OTSU)


            #     gray = cv2.cvtColor(cropped_image, cv2.COLOR_BGR2GRAY)
            #     gray = cv2.bitwise_not(gray)

            #     # threshold the image, setting all foreground pixels to
            #     # 255 and all background pixels to 0
            #     thresh = cv2.threshold(gray, 0, 255,
            #         cv2.THRESH_BINARY | cv2.THRESH_OTSU)[1]

            #     coords = np.column_stack(np.where(thresh > 0))
            #     angle = cv2.minAreaRect(coords)[-1]

            #     if angle < -45:
            #         angle = -(90 + angle)
            #     else:
            #         angle = -angle

            #     (h, w) = cropped_image.shape[:2]
            #     center = (w // 2, h // 2)
            #     M = cv2.getRotationMatrix2D(center, angle, 1.0)

            #     unwarped = th3

            #     cropped_image = cv2.warpAffine(cropped_image, M, (w, h),
            #         flags=cv2.INTER_CUBIC, borderMode=cv2.BORDER_REPLICATE)


            #    cv2.imwrite('test' + str(cnt) + '.jpg', th3)

                text = pytesseract.image_to_string(cropped_image, config='--tessdata-dir tessdata --psm 7', lang="spa")

                # use regex to strip whitespace and numbers
                text = text.strip()
                text = re.sub(r'[^\w\s]','',text)
                text = re.sub(r'\d+', '', text)
                text = text.lower()
                text_str.append(text)

            #condense list of blank spaces to one blank space or remove blank spaces

            if check(text_str) and text_str[0] == '':
                text_str = ''
            else:
                while '' in text_str:
                    text_str.remove('')

            if not text_str:
                text_str = ''

            print(text_str)
            text_array.append(text_str)


final_df = pd.DataFrame({"images":image_array,"text":text_array})
final_df.to_csv('final_df.csv')
