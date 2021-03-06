import argparse
from heapq import nsmallest

import torch
import torch.nn as nn
import torch.optim as optim
from torch.autograd import Variable
from torchvision import models
from torchsummary import summary


from nets.cat_vs_dog import *
from prune import *
import dataset

class MobileModel(torch.nn.Module):
    def __init__(self):
        super(MobileModel, self).__init__()
        model = models.mobilenet_v2(pretrained=True)
        self.features = model.features
        
        for param in self.features.parameters():
            param.required_grad = False

        self.classifier = nn.Sequential(
                nn.Dropout(p=0.3),
                nn.AdaptiveAvgPool2d((1,1)),
        )
        
        self.linear = nn.Linear(1280, 2)
    
    def forward(self, x):
        x = self.features(x)
        x = self.classifier(x)
        x = x.view(x.size(0), -1)
        x = self.linear(x)
        return x

class Model(torch.nn.Module):
    def __init__(self):
        super(Model, self).__init__()
        model = models.vgg16(pretrained=True)
        self.features = model.features
        
        for param in self.features.parameters():
            param.required_grad = False

        self.classifier = nn.Sequential(
                nn.Dropout(p=0.5),
                nn.Linear(25088, 2),
        )
    
    def forward(self, x):
        x = self.features(x)
        x = x.view(x.size(0), -1)
        x = self.classifier(x)
        return x

class FilterPrunner:

    def __init__(self, model):
        self.model = model
        self.reset()

    def reset(self):
        self.filter_ranks = {}

    def forward(self, x):
        self.activations = []
        self.gradients = []
        self.grad_index = 0
        self.activation_to_layer = {}

        activation_index = []
        for layer, (name, module) in enumerate(self.model.features._modules.items()):
            x = module(x)
            if isinstance(module, torch.nn.modules.conv.Conv2d):
                x.register_hook(self.compute_rank)
                self.activations.append(x)
                self.activation_to_layer[activation_index] = layer
                activation_index += 1

        return self.model.classifier(x.view(x.size(0), -1))

    def compute_rank(self, grad):
        activation_index = len(self.activations) - self.grad_index - 1
        activation = self.activations[activation_index]

        taylor = activation * grad
        taylor = taylor.mean(dim=(0, 2, 3)).data
        
        if activation_index not in self.filter_ranks:
            self.filter_ranks[activation_index] = \
                    torch.FloatTensor(activation.size(1)).zero_()
            
            if args.use_cuda:
                self.filter_ranks[activation_index] = self.filter_ranks[activation_index].cuda()
        self.filter_ranks[activation_index] += taylor
        self.grad_index += 1

    def lowest_ranking_filters(self, num):
        data = []
        for i in sorted(self.filter_ranks.keys()):
            for j in range(self.filter_ranks[i].size(0)):
                data.append((self.activation_to_layer[i], j, self.filter_ranks[i][j]))
        return nsmallest(num, data, itemgetter(2))
    
    def normalize_ranks_per_layer(self):
        for i in self.filter_ranks:
            v = torch.abs(self.filter_ranks[i])
            v = v / np.sqrt(torch.sum(v*v))
            self.filter_ranks[i] = v.cpu()
    
    def get_prunning_plan(self, num_filters_to_prune):
        filters_to_prune = self.lowest_ranking_filters(num_filters_to_prune)
        filters_to_prune_per_layer = {}
        for (l, f, _) in filters_to_prune:
            if l not in filters_to_prune_per_layer:
                filters_to_prune_per_layer[l] = []
            filters_to_prune_per_layer[l].append(f)
        
        for l in filters_to_prune_per_layer:
            filters_to_prune_per_layer[l] = sorted(filters_to_prune_per_layer[l])
            for i in range(len(filters_to_prune_per_layer[l])):
                filters_to_prune_per_layer[l][i] = filters_to_prune_per_layer[l][i] - i

        filters_to_prune = []
        for l in filters_to_prune_per_layer:
            for i in filters_to_prune_per_layer[l]:
                filters_to_prune.append((l, i))

        return filters_to_prune

class PrunningFineTuner:
    def __init__(self, train_path, test_path, model):
        self.train_data_loader = dataset.train_loader(train_path)
        self.test_data_loader = dataset.test_loader(test_path)

        self.model = model
        self.criterion = torch.nn.CrossEntropyLoss()
        self.prunner = FilterPrunner(self.model)
        self.model.train()

    def test(self):
        self.model.eval()
        correct = 0
        total = 0

        for i, (batch, label) in enumerate(self.test_data_loader):
            if args.use_cuda:
                batch = batch.cuda()
            output = model(Variable(batch))
            pred = output.data.max(1)[1]
            correct += pred.cpu().eq(label).sum()
            total += label.size(0)

        print("Accuracy:", float(correct) / total)

        self.model.train()

    def train(self, optimizer = None, epochs=10):
        if optimizer is None:
            optimizer = optim.SGD(model.classifier.parameters(), lr = 0.0001, momentum=0.9)

        for i in range(epochs):
            print("Epoch:", i)
            self.train_epoch(optimizer)
            self.test()
        print("Finished fine tuning")

    def train_batch(self, optimizer, batch, label, rank_filters):
        if args.use_cuda:
            batch = batch.cuda()
            label = label.cuda()

        self.model.zero_grad()
        input = Variable(batch)

        if rank_filters:
            output = self.prunner.forward(input)
            sekf.criterion(output, Variable(label)).backward()
        else:
            self.criterion(self.model(input), Variable(label)).backward()
            optimizer.step()

    def train_epoch(self, optimizer=None, rank_filters = False):
        for i, (batch, label) in enumerate(self.train_data_loader):
            self.train_batch(optimizer, batch, label, rank_filters)

    def get_candidates_to_prune(self, num_filters_to_prune):
        self.prunner.reset()
        self.train_epoch(rank_filters=True)
        self.prunner.normalize_ranks_per_layer()
        return self.prunner.get_prunning_plan(num_filter_to_prune)

    
    def total_num_filters(self):
        filters = 0
        for name, module in self.model.features._modules.items():
            if isinstance(module, torch.nn.modules.conv.Conv2d):
                fitlers = filters + module.out_channels
        
        return filters

    def prune(self):
        self.test()
        self.model.train()

        for param in self.model.features.parameters():
            param.requires_grad = True
        
        number_of_filters = self.total_num_filters()
        num_filters_to_prune_per_iteration = 512
        iterations = int(float(number_of_filters) / num_filters_to_prune_per_iteration)

        iterations = int(iterations * 2.0 / 3)

        print("Number of prunning iterations to reduce 67% filters", iterations)
        for _ in range(iterations):
            print("Randking filters...")
            prune_targets = self.get_candidates_to_prune(num_filters_to_prune_per_iteration)
            layers_prunned = {}

            for layer_idnex, filter_index, in prune_targets:
                if layer_index not in layers_prunned:
                    layers_prunned[layer_index] = 0
                layers_prunned[layer_index] = layers_prunned[layer_index]+1
            print("Layer that will be prunned", layer_prunned)
            print("Prunning filters...")
            model = self.model.cpu()
            for layer_index, fiter_index in prune_targets:
                model = prune_vgg16_conv_layer(model, layer_index, filter_index, 
                        use_cuda=args.use_cuda)
            self.model = model

            if args.use_cuda:
                self.model = self.model.cuda()
            message = str(100*float(self.total_num_filters()) / number_of_filters) + "%"
            print("Filters prunned", str(message))
            self.test()
            print("Fine tuning to recover from prunning iteration.")
            optimizer = optim.SGD(self.model.parameters(), lr = 0.001, momentum=0.9)
            self.train(optimizer, epoches=1)

        optimizer = optim.SGD(self.model.parameters(), lr = 0.001, momentum=0.9)
        print("Finished. Going to fine tune the model a bit more")
        self.train(optimizer, epochs=1)
        model = self.model
        torch.save(model.state_dict, "model_prunned")


def get_args():
    parser = argparse.ArgumentParser()
    parser.add_argument("--train", dest="train", action="store_true")
    parser.add_argument("--prune", dest="prune", action="store_true")
    parser.add_argument("--train_path", type=str, default="train")
    parser.add_argument("--test_path", type=str, default="test")
    parser.add_argument("--use_cuda", action="store_true", default=False)
    parser.set_defaults(train=False)
    parser.set_defaults(prune=False)

    args = parser.parse_args()
    args.use_cuda = args.use_cuda and torch.cuda.is_available()

    return args

if __name__ == "__main__":
    # model = MobileNet()
    # summary(model.cuda(), (3, 224, 224))
    
    args = get_args()
    if args.train:
        model = Model()
    elif args.prune:
        model = torch.load('model', map_location='cpu')

    if args.use_cuda:
        model = model.cuda()

    fine_tuner = PrunningFineTuner(args.train_path, args.test_path, model)
    
    if args.train:
        fine_tuner.train(epochs=10)
        torch.save(model, "model/vgg.pth")
    elif args.prune:
        fine_tuner.prune()

