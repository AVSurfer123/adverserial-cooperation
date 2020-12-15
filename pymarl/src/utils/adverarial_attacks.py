import math
import torch
from torch.autograd.gradcheck import zero_gradients


def fast_compute_jacobian(net, hidden, x, output):
	assert x.requires_grad

	x = x.squeeze()
	noutputs = output.size()[1]
	n = x.size()[0]
	x = x.repeat(noutputs, 1)
	hidden = hidden.repeat(noutputs,1)
	x.requires_grad_(True)
	x.retain_grad()
	hidden.requires_grad_(True)
	hidden.retain_grad()

	y, hidden = net(x, hidden)
	grad_output = torch.eye(noutputs)
	if x.is_cuda:
		grad_output = grad_output.cuda()
	y.backward(grad_output, retain_graph=True)
	return x.grad.data.view(1,*x.grad.data.shape)


def compute_jacobian(inputs, output):
	"""
	:param inputs: Batch X Size (e.g. Depth X Width X Height)
	:param output: Batch X Classes
	:return: jacobian: Batch X Classes X Size
	"""
	assert inputs.requires_grad

	num_classes = output.size()[1]

	jacobian = torch.zeros(num_classes, *inputs.size())
	grad_output = torch.zeros(*output.size())
	if inputs.is_cuda:
		grad_output = grad_output.cuda()
		jacobian = jacobian.cuda()

	for i in range(num_classes):
		zero_gradients(inputs)
		grad_output.zero_()
		grad_output[:, i] = 1
		output.backward(grad_output, retain_graph=True)
		jacobian[i] = inputs.grad.data

	return torch.transpose(jacobian, dim0=0, dim1=1)


def fgsm(inputs, targets, model, criterion, eps):
	"""
	:param inputs: Clean samples (Batch X Size)
	:param targets: True labels
	:param model: Model
	:param criterion: Loss function
	:param gamma:
	:return:
	"""

	crafting_input = torch.autograd.Variable(inputs.clone(), requires_grad=True)
	crafting_target = torch.autograd.Variable(targets.clone())
	output = model(crafting_input)
	loss = criterion(output, crafting_target)
	if crafting_input.grad is not None:
		crafting_input.grad.data.zero_()
	loss.backward()
	crafting_output = crafting_input.data + eps*torch.sign(crafting_input.grad.data)

	return crafting_output


def saliency_map(jacobian, search_space, target_index, increasing=True):
	all_sum = torch.sum(jacobian, 1).squeeze()
	alpha = jacobian[:,target_index].squeeze()
	beta = all_sum - alpha

	# OK instead lets find the two best features
	if increasing:
		mask1 = torch.ge(alpha, 0.0)
		mask2 = torch.le(beta, 0.0)
	else:
		mask1 = torch.le(alpha, 0.0)
		mask2 = torch.ge(beta, 0.0)

	mask = torch.mul(torch.mul(mask1, mask2), search_space)

	if increasing:
		saliency_map = torch.mul(torch.mul(alpha, torch.abs(beta)), mask.float())
	else:
		saliency_map = torch.mul(torch.mul(torch.abs(alpha), beta), mask.float())

	max_value, max_idx = torch.max(saliency_map, dim=0)

	return max_value, max_idx


def bidirectional_saliency_map(jacobian, search_space, target_index):
	d = 0
	all_sum = torch.sum(jacobian, 1).squeeze()
	alpha = jacobian[:,target_index].squeeze()

	all_sum_combinations = all_sum[:,None]+all_sum[None, :]
	alpha_combinations = alpha[:,None]+alpha[None, :]
	beta_combinations = all_sum_combinations-alpha_combinations
	
	scores = alpha_combinations*beta_combinations
	scores[scores > 0] = 0
	scores = torch.abs(scores)
	row_max, row_indices = torch.max(scores, dim=0)
	col_idx = torch.argmax(row_max)
	i, j = row_indices[col_idx], col_idx
	if alpha_combinations[i,j] != 0:
		d = math.copysign(1,alpha_combinations[i,j])
	return i,j,d


# TODO: Currently, assuming one sample at each time
def jsma(model, hidden_states, input_tensor, target_class, max_distortion=0.1):

	# Make a clone since we will alter the values
	input_features = torch.autograd.Variable(input_tensor.clone(), requires_grad=True)
	num_features = input_features.size(1)
	max_iter = math.floor(num_features * max_distortion)
	count = 0

	# a mask whose values are one for feature dimensions in search space
	search_space = torch.ones(num_features).byte()
	if input_features.is_cuda:
		search_space = search_space.cuda()

	output, _ = model(input_features, hidden_states)
	_, source_class = torch.max(output.data, 1)
	theta = 0.1
	#max_theta = 0.9
	#original_class = source_class.item()
	while (count < max_iter) and (source_class.item() != target_class[0]) and (search_space.sum() != 0):
		jacobian = fast_compute_jacobian(model, hidden_states, input_features, output)

		increasing_saliency_value, increasing_feature_index = saliency_map(jacobian, search_space, target_class, increasing=True)
		mask_zero = torch.gt(input_features.data.squeeze(), 0.0)
		search_space_decreasing = torch.mul(mask_zero, search_space)
		decreasing_saliency_value, decreasing_feature_index = saliency_map(jacobian, search_space_decreasing, target_class, increasing=False)
		
		if increasing_saliency_value.item() == 0.0 and decreasing_saliency_value.item() == 0.0:
			i,j,d = bidirectional_saliency_map(jacobian,search_space,target_class)
			if d==0:
				print("yikes")
				break
			input_features.data[0][i]+=theta*d
			input_features.data[0][j]+=theta*d

		if increasing_saliency_value.item() > decreasing_saliency_value.item():
			input_features.data[0][increasing_feature_index] += theta
		else:
			input_features.data[0][decreasing_feature_index] -= theta
		
		output, _ = model(input_features, hidden_states)
		_, source_class = torch.max(output.data, 1)

		count += 1
		theta += 0.1 
	#print(source_class.item(), target_class[0])
	return input_features


	