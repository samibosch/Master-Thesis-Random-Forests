from sklearn.base import BaseEstimator, ClassifierMixin
from sklearn.utils.validation import check_X_y
from sklearn.utils import resample
import numpy as np
from dl85 import DL85Classifier
from gurobipy import Model, GRB, quicksum


class Forest(BaseEstimator, ClassifierMixin):
    def __init__(self,
                 n_estimators=10,
                 tree_class=DL85Classifier,
                 method="random",
                 attributes="all",
                 n_samples=25,
                 sampling_type="%",
                 optimised=False,
                 **kwargs):
        self.estimators = []
        self.all_estimators = []
        self.n_estimators = n_estimators
        self.tree_class = tree_class
        self.n_samples = n_samples
        self.sampling_type = sampling_type
        self.kwargs = kwargs
        self.is_fitted = False
        self.unanimity = None
        self.method = method
        self.attributes = attributes
        self.weights = []
        self.all_weights = []
        self.optimised = optimised

    def fit(self, X, y):
        check_X_y(X, y)
        orig_X = np.array(X)
        self.estimators = []
        self.weights = []

        sample_size = round(self.n_samples / 100 * len(X)) if self.sampling_type == "%" else self.n_samples
        column_size = round(75 / 100 * len(X[0]))

        for i in range(self.n_estimators):
            tree = self.tree_class(**self.kwargs)
            self.estimators.append(tree)

            if self.method == "random":
                sample, classes = resample(X, y, n_samples=sample_size)
            else:
                sample, classes = X, y

            if self.attributes == "random":
                # TODO: fix
                sample = resample(*sample, n_samples=column_size, replace=False)

            tree.fit(sample, classes)

            if self.attributes == "progressive":
                root = tree.tree_

                attr = root['feat']
                for i in range(len(X)):
                    X[i][attr] = 1

            if self.attributes == "random_progressive":
                root = tree.tree_
                pred = None
                while 'class' not in root:
                    c = np.random.randint(0, 2)
                    pred = root
                    if c == 0:
                        break
                    elif c == 1:
                        root = root['left']
                    else:
                        root = root['right']

                attr = pred['feat']
                for i in range(len(X)):
                    X[i][attr] = 1

        X = orig_X

        if self.optimised and self.tree_class == DL85Classifier:
            pred = np.array([t.predict(X) for t in self.estimators]) * 2 - 1

            cont = True
            tree_count = self.n_estimators
            sample_count = len(y)
            c = np.array(y) * 2 - 1

            prev_sample_weights = None

            while cont:
                sample_weights, gamma = calculate_sample_weights(pred, c, prev_sample_weights)
                prev_sample_weights = sample_weights

                def error(tids):
                    all_classes = [0, 1]
                    supports = [0, 0]
                    for i in tids:
                        supports[(c[i] + 1) // 2] += int(sample_count * sample_weights[i])
                    maxindex = np.argmax(supports)
                    return sum(supports) - supports[maxindex], all_classes[maxindex]

                tree = self.tree_class(error_function=error, **self.kwargs)
                tree.fit(X, y)

                for t in self.estimators:
                    if tree.tree_ == t.tree_:
                        cont = False

                tree_pred = np.array(tree.predict(X)) * 2 - 1
                accuracy = sum(c * sample_weights * tree_pred)
                print(accuracy, tree.accuracy_, gamma)

                if accuracy > gamma and cont:
                    self.estimators.append(tree)
                    pred = np.vstack([pred, tree_pred])
                    tree_count += 1
                else:
                    tree_weights, _ = calculate_tree_weights(pred, c)

                    self.all_weights = tree_weights
                    self.all_estimators = list(self.estimators)
                    self.estimators = []
                    for w, e in zip(self.all_weights, self.all_estimators):
                        if w != 0:
                            self.weights.append(w)
                            self.estimators.append(e)
                    cont = False

        self.is_fitted = True
        return self

    def predict(self, X):
        lst = np.array([t.predict(X) for t in self.estimators])
        if self.optimised:
            lst = lst * 2 - 1
            wlst = [self.weights[t] * lst[t, :] for t in range(len(self.estimators))]
            pred = np.sum(wlst, axis=0)
            pred = [0 if p < 0 else 1 for p in pred]
            self.unanimity = [np.count_nonzero(lst[:, i] == pred[i]) for i in range(len(lst[0]))]
            return pred
        else:
            pred = [np.argmax(np.bincount(lst[:, i])) for i in range(len(lst[0]))]
            self.unanimity = [np.count_nonzero(lst[:, i] == pred[i]) for i in range(len(lst[0]))]
            return pred

    def check_is_fitted(self):
        return self.is_fitted

    def get_depth_map(self):
        if self.tree_class == DL85Classifier:
            depth_map = {}
            for t in self.estimators:
                tree = t.tree_

                def build_depth_map(curr_tree, n=1):
                    if n not in depth_map:
                        depth_map[n] = {}
                    d = depth_map[n]
                    if 'class' not in curr_tree:
                        f = curr_tree['feat']
                        if f not in d:
                            d[f] = 1
                        else:
                            d[f] += 1
                        build_depth_map(curr_tree['left'], n + 1)
                        build_depth_map(curr_tree['right'], n + 1)

                build_depth_map(tree)
            return depth_map
        else:
            return None

    def get_unanimity(self):
        return self.unanimity

    def get_n_estimators(self):
        return self.n_estimators


def calculate_sample_weights(pred, c, prev_sample_weights=None):
    tree_count = len(pred)
    sample_count = len(c)

    m = Model("sample_weight_optimiser")
    sample_weights = [m.addVar(vtype=GRB.CONTINUOUS, name="sample_weights " + str(i))
                      for i in range(sample_count)]
    if prev_sample_weights is not None:
        for i in range(len(prev_sample_weights)):
            sample_weights[i].setAttr("Start", prev_sample_weights[i])

    gamma = m.addVar(vtype=GRB.CONTINUOUS, name="gamma", lb=float("-inf"))

    m.setObjective(gamma, GRB.MINIMIZE)

    m.addConstr(quicksum(sample_weights) == 1, name="weights = 1")
    for t in range(tree_count):
        m.addConstr(
            quicksum([c[i] * sample_weights[i] * pred[t, i] for i in range(sample_count)]) <= gamma,
            name="Constraint on tree " + str(t))

    m.setParam("LogToConsole", 0)
    m.optimize()
    print(sample_weights)
    print(gamma)

    return [w.X for w in sample_weights], gamma.X


def calculate_tree_weights(pred, c):
    tree_count = len(pred)
    sample_count = len(c)

    m1 = Model("tree_weight_optimiser")
    tree_weights = [m1.addVar(vtype=GRB.CONTINUOUS, name="tree_weights " + str(t)) for t in
                    range(tree_count)]
    rho = m1.addVar(vtype=GRB.CONTINUOUS, name="rho", lb=float("-inf"))

    m1.setObjective(rho, GRB.MAXIMIZE)

    m1.addConstr(quicksum(tree_weights) == 1, name="weights = 1")
    for i in range(sample_count):
        m1.addConstr(quicksum([c[i] * tree_weights[t] * pred[t, i] for t in range(tree_count)]) >= rho,
                     name="Constraint on sample " + str(i))

    m1.setParam("LogToConsole", 0)
    m1.optimize()
    print(tree_weights)
    print(rho)

    return [w.X for w in tree_weights], rho.X
