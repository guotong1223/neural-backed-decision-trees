from models.trees import TreeSup
from utils.graph import get_root, get_wnids
from utils.utils import (
    DEFAULT_CIFAR10_TREE, DEFAULT_CIFAR10_WNIDS, DEFAULT_CIFAR100_TREE,
    DEFAULT_CIFAR100_WNIDS, DEFAULT_TINYIMAGENET200_TREE,
    DEFAULT_TINYIMAGENET200_WNIDS, DEFAULT_IMAGENET1000_TREE,
    DEFAULT_IMAGENET1000_WNIDS,
)
from utils.data.custom import Node
import torch
import torch.nn as nn
import numpy as np
import csv


__all__ = names = (
    'Noop', 'ConfusionMatrix', 'ConfusionMatrixJointNodes',
    'IgnoredSamples', 'DecisionTreePrior', 'CIFAR10DecisionTreePrior',
    'CIFAR100DecisionTreePrior', 'TinyImagenet200DecisionTreePrior',
    'Imagenet1000DecisionTreePrior', 'DecisionTreeBayesianPrior',
    'CIFAR10DecisionTreeBayesianPrior', 'CIFAR100DecisionTreeBayesianPrior',
    'TinyImagenet1000DecisionTreeBayesianPrior', 'Imagenet1000DecisionTreeBayesianPrior')


class Noop:

    def __init__(self, trainset, testset):
        self.trainset = trainset
        self.testset = testset

        self.epoch = None

    def start_epoch(self, epoch):
        self.epoch = epoch

    def start_train(self, epoch):
        assert epoch == self.epoch

    def update_batch(self, outputs, predicted, targets):
        pass

    def end_train(self, epoch):
        assert epoch == self.epoch

    def start_test(self, epoch):
        assert epoch == self.epoch

    def end_test(self, epoch):
        assert epoch == self.epoch

    def end_epoch(self, epoch):
        assert epoch == self.epoch


class ConfusionMatrix(Noop):

    def __init__(self, trainset, testset):
        super().__init__(trainset, testset)
        self.k = len(trainset.classes)
        self.m = None

    def start_train(self, epoch):
        super().start_train(epoch)
        raise NotImplementedError()

    def start_test(self, epoch):
        super().start_test(epoch)
        self.m = np.zeros((self.k, self.k))

    def update_batch(self, outputs, predicted, targets):
        super().update_batch(outputs, predicted, targets)
        if len(predicted.shape) == 1:
            predicted = predicted.numpy().ravel()
            targets = targets.numpy().ravel()
            ConfusionMatrix.update(self.m, predicted, targets)

    def end_test(self, epoch):
        super().end_test(epoch)
        recall = self.recall()
        for row, cls in zip(recall, self.trainset.classes):
            print(row, cls)
        print(recall.diagonal(), '(diagonal)')

    @staticmethod
    def update(confusion_matrix, preds, labels):
        preds = tuple(preds)
        labels = tuple(labels)

        for pred, label in zip(preds, labels):
            confusion_matrix[label, pred] += 1

    @staticmethod
    def normalize(confusion_matrix, axis):
        total = confusion_matrix.astype(np.float).sum(axis=axis)
        total = total[:, None] if axis == 1 else total[None]
        return confusion_matrix / total

    def recall(self):
        return ConfusionMatrix.normalize(self.m, 1)

    def precision(self):
        return ConfusionMatrix.normalize(self.m, 0)


class ConfusionMatrixJointNodes(ConfusionMatrix):
    """Calculates confusion matrix for tree of joint nodes"""

    def __init__(self, trainset, testset):
        assert hasattr(trainset, 'nodes'), (
            'Dataset must be for joint nodes, in order to run joint-node '
            'specific confusion matrix analysis. You can run the regular '
            'confusion matrix analysis instead.'
        )
        self.nodes = trainset.nodes

    def start_test(self, epoch):
        self.ms = [
            np.zeros((node.num_classes, node.num_classes))
            for node in self.nodes
        ]

    def update_batch(self, outputs, predicted, targets):
        for m, pred, targ in zip(self.ms, predicted.T, targets.T):
            pred = pred.numpy().ravel()
            targ = targ.numpy().ravel()
            ConfusionMatrix.update(m, pred, targ)

    def end_test(self, epoch):
        mean_accs = []

        for m, node in zip(self.ms, self.nodes):
            class_accs = ConfusionMatrix.normalize(m, 0).diagonal()
            mean_acc = np.mean(class_accs)
            print(node.wnid, node.classes, mean_acc, class_accs)
            mean_accs.append(mean_acc)

        min_acc = min(mean_accs)
        min_node = self.nodes[mean_accs.index(min_acc)]
        print(f'Node ({min_node.wnid}) with lowest accuracy ({min(mean_accs)}%)'
              f' (sorted accuracies): {sorted(mean_accs)}')

class IgnoredSamples(Noop):
    """ Counter for number of ignored samples in decision tree """

    def __init__(self, trainset, testset):
        super().__init__(trainset, testset)
        self.ignored = None

    def start_test(self, epoch):
        super().start_test(epoch)
        self.ignored = 0

    def update_batch(self, outputs, predicted, targets):
        super().update_batch(outputs, predicted, targets)
        self.ignored += outputs[:,0].eq(-1).sum().item()

    def end_test(self, epoch):
        super().end_test(epoch)
        print("Ignored Samples: {}".format(self.ignored))


class DecisionTreePrior(Noop):
    """Evaluate model on decision tree prior. Evaluation is deterministic."""

    accepts_path_graph_analysis = True

    def __init__(self, trainset, testset, path_graph_analysis, path_wnids):
        super().__init__(trainset, testset)
        self.nodes = Node.get_nodes(path_graph_analysis, path_wnids, trainset.classes)
        self.G = self.nodes[0].G
        self.wnid_to_node = {node.wnid: node for node in self.nodes}

        self.wnids = get_wnids(path_wnids)
        self.classes = trainset.classes
        self.wnid_to_class = {wnid: cls for wnid, cls in zip(self.wnids, self.classes)}

        self.correct = 0
        self.total = 0

    def update_batch(self, outputs, predicted, targets):
        super().update_batch(outputs, predicted, targets)

        targets_ints = [int(target) for target in targets.cpu().long()]
        wnid_to_pred_selector = {}
        for node in self.nodes:
            selector, outputs_sub, targets_sub = TreeSup.inference(node, outputs, targets)
            if not any(selector):
                continue
            _, preds_sub = torch.max(outputs_sub, dim=1)
            preds_sub = list(map(int, preds_sub.cpu()))
            wnid_to_pred_selector[node.wnid] = (preds_sub, selector)

        n_samples = outputs.size(0)
        predicted = self.traverse_tree(
            predicted, wnid_to_pred_selector, n_samples).to(targets.device)
        self.total += n_samples
        self.correct += (predicted == targets).sum().item()
        accuracy = round(self.correct / float(self.total), 4) * 100
        return f'TreePrior: {accuracy}%'

    def traverse_tree(self, _, wnid_to_pred_selector, n_samples):
        wnid_root = get_root(self.G)
        node_root = self.wnid_to_node[wnid_root]
        preds = []
        for index in range(n_samples):
            wnid, node = wnid_root, node_root
            while node is not None:
                if node.wnid not in wnid_to_pred_selector:
                    wnid = node = None
                    break
                pred_sub, selector = wnid_to_pred_selector[node.wnid]
                if not selector[index]:  # we took a wrong turn. wrong.
                    wnid = node = None
                    break
                index_new = sum(selector[:index + 1]) - 1
                index_child = pred_sub[index_new]
                wnid = node.children[index_child]
                node = self.wnid_to_node.get(wnid, None)
            cls = self.wnid_to_class.get(wnid, None)
            pred = -1 if cls is None else self.classes.index(cls)
            preds.append(pred)
        return torch.Tensor(preds).long()

    def end_test(self, epoch):
        super().end_test(epoch)
        accuracy = round(self.correct / self.total * 100., 2)
        print(f'TreePrior Accuracy: {accuracy}%, {self.correct}/{self.total}')


class CIFAR10DecisionTreePrior(DecisionTreePrior):

    def __init__(self, trainset, testset,
        path_graph_analysis=DEFAULT_CIFAR10_TREE,
        path_wnids=DEFAULT_CIFAR10_WNIDS):
        super().__init__(trainset, testset, path_graph_analysis, path_wnids)


class CIFAR100DecisionTreePrior(DecisionTreePrior):

    def __init__(self, trainset, testset,
        path_graph_analysis=DEFAULT_CIFAR100_TREE,
        path_wnids=DEFAULT_CIFAR100_WNIDS):
        super().__init__(trainset, testset, path_graph_analysis, path_wnids)


class TinyImagenet200DecisionTreePrior(DecisionTreePrior):

    def __init__(self, trainset, testset,
        path_graph_analysis=DEFAULT_TINYIMAGENET200_TREE,
        path_wnids=DEFAULT_TINYIMAGENET200_WNIDS):
        super().__init__(trainset, testset, path_graph_analysis, path_wnids)


class Imagenet1000DecisionTreePrior(DecisionTreePrior):

    def __init__(self, trainset, testset,
        path_graph_analysis=DEFAULT_IMAGENET1000_TREE,
        path_wnids=DEFAULT_IMAGENET1000_WNIDS):
        super().__init__(trainset, testset, path_graph_analysis, path_wnids)


class DecisionTreeBayesianPrior(DecisionTreePrior):
    """Evaluate model on decision tree bayesian prior. Evaluation is stochastic."""

    accepts_path_graph_analysis = True

    def __init__(self, trainset, testset, path_graph_analysis, path_wnids):
        super().__init__(trainset, testset, path_graph_analysis, path_wnids)
        self.softmax = nn.Softmax(dim=1)

    def update_batch(self, outputs, predicted, targets):
        wnid_to_output = {}
        for node in self.nodes:
            node_outputs = torch.stack([
                outputs.T[node.new_to_old_classes[new_label]].mean(dim=0)
                for new_label in range(node.num_classes)
            ]).T
            wnid_to_output[node.wnid] = node_outputs.cpu()

        n_samples = outputs.size(0)
        predicted = self.traverse_tree(
            predicted, wnid_to_output, n_samples).to(targets.device)
        self.total += n_samples
        self.correct += (predicted == targets).sum().item()
        accuracy = round(self.correct / float(self.total), 4) * 100
        return f'TreeBayesianPrior: {accuracy}%'

    def traverse_tree(self, _, wnid_to_output, n_samples):
        class_probs = np.ones((n_samples, len(self.classes)))
        for node in self.nodes:
            output = wnid_to_output[node.wnid]
            output = self.softmax(output).numpy()
            for index_child in range(len(node.children)):
                old_indexes = node.new_to_old_classes[index_child]
                class_probs[:,old_indexes] *= output[:,index_child:index_child+1]
        preds = list(np.argmax(class_probs, axis=1))
        return torch.Tensor(preds).long()

    def end_test(self, epoch):
        accuracy = round(self.correct / self.total * 100., 2)
        print(f'TreeBayesianPrior Accuracy: {accuracy}%, {self.correct}/{self.total}')


class CIFAR10DecisionTreeBayesianPrior(DecisionTreeBayesianPrior):

    def __init__(self, trainset, testset,
        path_graph_analysis=DEFAULT_CIFAR10_TREE,
        path_wnids=DEFAULT_CIFAR10_WNIDS):
        super().__init__(trainset, testset, path_graph_analysis, path_wnids)


class CIFAR100DecisionTreeBayesianPrior(DecisionTreeBayesianPrior):

    def __init__(self, trainset, testset,
        path_graph_analysis=DEFAULT_CIFAR100_TREE,
        path_wnids=DEFAULT_CIFAR100_WNIDS):
        super().__init__(trainset, testset, path_graph_analysis, path_wnids)


class TinyImagenet200DecisionTreeBayesianPrior(DecisionTreeBayesianPrior):

    def __init__(self, trainset, testset,
        path_graph_analysis=DEFAULT_TINYIMAGENET200_TREE,
        path_wnids=DEFAULT_TINYIMAGENET200_WNIDS):
        super().__init__(trainset, testset, path_graph_analysis, path_wnids)


class Imagenet1000DecisionTreeBayesianPrior(DecisionTreeBayesianPrior):

    def __init__(self, trainset, testset,
        path_graph_analysis=DEFAULT_IMAGENET1000_TREE,
        path_wnids=DEFAULT_IMAGENET1000_WNIDS):
        super().__init__(trainset, testset, path_graph_analysis, path_wnids)
