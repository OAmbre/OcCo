#  Copyright (c) 2020. Hanchen Wang, hw501@cam.ac.uk
#  Ref: https://github.com/wentaoyuan/pcn/blob/master/models/pcn_cd.py
#  Ref: https://github.com/AnTao97/UnsupervisedPointCloudReconstruction/blob/master/model.py

import pdb, sys, torch, itertools, numpy as np, torch.nn as nn, torch.nn.functional as F
from dgcnn_util import get_graph_feature
sys.path.append("../chamfer_distance")
from chamfer_distance import ChamferDistance


class get_model(nn.Module):
	def __init__(self, **kwargs):
		super(get_model, self).__init__()

		self.grid_size = 4
		self.grid_scale = 0.5
		self.num_coarse = 1024
		self.device = torch.device('cuda' if torch.cuda.is_available() else 'cpu')
		self.__dict__.update(kwargs)  # to update args, num_coarse, grid_size, grid_scale

		self.num_fine = self.grid_size ** 2 * self.num_coarse  # 16384
		self.meshgrid = [[-self.grid_scale, self.grid_scale, self.grid_size],
						 [-self.grid_scale, self.grid_scale, self.grid_size]]

		# DGCNN Encoder Step-by-Step
		self.bn1 = nn.BatchNorm2d(64)
		self.bn2 = nn.BatchNorm2d(64)
		self.bn3 = nn.BatchNorm2d(128)
		self.bn4 = nn.BatchNorm2d(256)
		self.bn5 = nn.BatchNorm1d(self.args.emb_dims)

		self.conv1 = nn.Sequential(nn.Conv2d(6, 64, kernel_size=1, bias=False),
								   self.bn1,
								   nn.LeakyReLU(negative_slope=0.2))
		self.conv2 = nn.Sequential(nn.Conv2d(64*2, 64, kernel_size=1, bias=False),
								   self.bn2,
								   nn.LeakyReLU(negative_slope=0.2))
		self.conv3 = nn.Sequential(nn.Conv2d(64*2, 128, kernel_size=1, bias=False),
								   self.bn3,
								   nn.LeakyReLU(negative_slope=0.2))
		self.conv4 = nn.Sequential(nn.Conv2d(128*2, 256, kernel_size=1, bias=False),
								   self.bn4,
								   nn.LeakyReLU(negative_slope=0.2))
		self.conv5 = nn.Sequential(nn.Conv1d(512, self.args.emb_dims, kernel_size=1, bias=False),
								   self.bn5,
								   nn.LeakyReLU(negative_slope=0.2))

		self.folding1 = nn.Sequential(
			nn.Linear(self.args.emb_dims, 1024),
			nn.BatchNorm1d(1024),
			nn.ReLU(),
			nn.Linear(1024, 1024),
			nn.BatchNorm1d(1024),
			nn.ReLU(),
			nn.Linear(1024, self.num_coarse * 3))

		self.folding2 = nn.Sequential(
			nn.Conv1d(1024+2+3, 512, 1),
			nn.BatchNorm1d(512),
			nn.ReLU(),
			nn.Conv1d(512, 512, 1),
			nn.BatchNorm1d(512),
			nn.ReLU(),
			nn.Conv1d(512, 3, 1))

	def build_grid(self, batch_size):

		x, y = np.linspace(*self.meshgrid[0]), np.linspace(*self.meshgrid[1])
		points = np.array(list(itertools.product(x, y)))
		points = np.repeat(points[np.newaxis, ...], repeats=batch_size, axis=0)
		
		return torch.tensor(points).float().to(self.device)

	def tile(self, tensor, multiples):
		# substitute for tf.tile: 
		# https://www.tensorflow.org/versions/r1.15/api_docs/python/tf/tile
		# Ref: https://discuss.pytorch.org/t/how-to-tile-a-tensor/13853/3
		def tile_single_axis(a, dim, n_tile):
			init_dim = a.size(dim)
			repeat_idx = [1] * a.dim()
			repeat_idx[dim] = n_tile
			a = a.repeat(*repeat_idx)
			order_index = torch.Tensor(
				np.concatenate([init_dim * np.arange(n_tile) + i for i in range(init_dim)])).long()
			return torch.index_select(a, dim, order_index.to(self.device))

		for dim, n_tile in enumerate(multiples):
			if n_tile == 1:
				continue
			tensor = tile_single_axis(tensor, dim, n_tile)
		return tensor

	@staticmethod
	def expand_dims(tensor, dim):
		# substitute for tf.expand_dims: 
		# https://www.tensorflow.org/versions/r1.15/api_docs/python/tf/expand_dims
		return tensor.unsqueeze(-1).transpose(-1, dim)

	def forward(self, x):

		batch_size = x.size()[0]
		x = get_graph_feature(x, k=self.args.k)
		x = self.conv1(x)
		x1 = x.max(dim=-1, keepdim=False)[0]

		x = get_graph_feature(x1, k=self.args.k)
		x = self.conv2(x)
		x2 = x.max(dim=-1, keepdim=False)[0]

		x = get_graph_feature(x2, k=self.args.k)
		x = self.conv3(x)
		x3 = x.max(dim=-1, keepdim=False)[0]

		x = get_graph_feature(x3, k=self.args.k)
		x = self.conv4(x)
		x4 = x.max(dim=-1, keepdim=False)[0]

		x = torch.cat((x1, x2, x3, x4), dim=1)

		x = self.conv5(x)
		feature = F.adaptive_max_pool1d(x, 1).view(batch_size, -1)
		# x1 = F.adaptive_max_pool1d(x, 1).view(batch_size, -1)
		# x2 = F.adaptive_avg_pool1d(x, 1).view(batch_size, -1)
		# feature = torch.cat((x1, x2), 1)

		coarse = self.folding1(feature)
		coarse = coarse.view(-1, self.num_coarse, 3)

		grid = self.build_grid(x.size()[0])
		# grid_feat = self.tile(grid, [1, self.num_coarse, 1])
		grid_feat = grid.repeat(1, self.num_coarse, 1)

		point_feat = self.tile(self.expand_dims(coarse, 2), [1, 1, self.grid_size ** 2, 1])
		point_feat = point_feat.view([-1, self.num_fine, 3])
		
		global_feat = self.tile(self.expand_dims(feature, 1), [1, self.num_fine, 1])	
		feat = torch.cat([grid_feat, point_feat, global_feat], dim=2)
	
		center = self.tile(self.expand_dims(coarse, 2), [1, 1, self.grid_size ** 2, 1])
		center = center.view([-1, self.num_fine, 3])

		fine = self.folding2(feat.transpose(2, 1)).transpose(2, 1) + center

		return coarse, fine


class get_loss(nn.Module):
	def __init__(self):
		super(get_loss, self).__init__()

	@staticmethod
	def dist_cd(pc1, pc2):

		chamfer_dist = ChamferDistance()
		dist1, dist2 = chamfer_dist(pc1, pc2)
		
		return torch.mean(dist1) + torch.mean(dist2)

	def forward(self, coarse, fine, gt, alpha):

		return self.dist_cd(coarse, gt) + alpha * self.dist_cd(fine, gt)


if __name__ == '__main__':

	model = get_model()
	print(model)
	input_pc = torch.rand(7, 3, 1024)
	x = model(input_pc)
