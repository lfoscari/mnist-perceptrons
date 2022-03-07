from kmeans_pytorch import kmeans
from functools import partial
from tqdm import tqdm
import shutil
import torch
import json
import time
import os

from utils import *
from MNIST import label_set, batch_data_iter
from MultilabelKernelPerceptron import MultilabelKernelPerceptron

if torch.cuda.is_available():
    DEVICE = torch.device("cuda")
    torch.set_default_tensor_type("torch.cuda.FloatTensor")
else:
    DEVICE = torch.device("cpu")
    torch.set_default_tensor_type("torch.FloatTensor")

TRAINING_SET_SIZE = 60_000
TEST_SET_SIZE = 10_000

DATASET_TEMPORARY_LOCATION = "/tmp/kmmkp-dataset-sketching"
DATASET_LOCATION = "./dataset"
RESULTS_LOCATION = "./results"

REDUCTIONS = [200, 1000, 1500]

torch.manual_seed(SEED)


def compress(xs, ys, target_size):
    """
    Splits the training data according to the label into clusters
    and then find the right number of centers for each cluster.
    Given that each cluster has an associated label, the resulting
    centers will be classified with such label.
    """

    # Sort x_train by label in y_train
    _, indices = ys.sort()
    sorted_x_train = xs[indices]

    # Count the occurrences of each label
    label_amount = ys.bincount()

    # Split the training points in one of 10 buckets according to their label
    label_split = sorted_x_train.split(tuple(label_amount))

    # Compute centroid set for each bucket
    centers = {label: None for label in label_set}
    bucket_sizes = []

    for label, bucket in enumerate(label_split):
        # If L is the amount of data-points with a particular label in the dataset with size N,
        # we want to reduce the total size from N to N' we want the ratio L/N to stay
        # roughly the same when reducing, therefore we ask that L' / N' =~ L / N, to find the
        # new amount of data-points with the given label we must solve for L':
        #   L' = L * N' / N
        # Pick at least 2 centers for cluster for good measure.
        centers_amount = max(2, bucket.shape[0] * target_size // xs.shape[0])
        centers[label] = kmeans(bucket, centers_amount, device=DEVICE)
        bucket_sizes.append(centers_amount)

    # Create the new training set by joining the buckets, labeling the data
    xs_km = torch.cat([c for _, c in centers.values()])
    ys_km = torch.cat(
        [torch.empty(centers_amount).fill_(label) for centers_amount, label in zip(bucket_sizes, label_set)])

    # Shuffle everything
    permutation = torch.randperm(xs_km.shape[0], device=DEVICE)
    xs_km = xs_km[permutation]
    ys_km = ys_km[permutation]

    # Alternative approach
    # This one simply sets a k and uses k-means on the whole dataset, then
    # assigns to each center the most common label in its circle.
    # Problem: it will be killed by the OS even with 5000 examples

    # x_train_km, indices = kmeans(x_train, math.ceil(root))
    #
    # y_train_km = torch.empty(x_train.shape[0])
    # for index in range(x_train_km.shape[0]):
    # 	members = [point_index for point_index, center_index in enumerate(range(x_train.shape[0])) if center_index == index]
    # 	labels = y_train[members]
    # 	y_train_km[index] = max(set(labels), key=labels.count)

    return xs_km, ys_km


def compress_dataset():
    """
    Loads the original dataset from PyTorch and applies a sketching method based on K-means.
    Multiple reductions are tested. The results are saved in 'dataset'.
    """

    sketching_time = {r: None for r in REDUCTIONS}

    if os.path.exists(DATASET_LOCATION):
        print("Dataset already downloaded and compressed... skipping")
        return

    if os.path.exists(DATASET_TEMPORARY_LOCATION):
        shutil.rmtree(DATASET_TEMPORARY_LOCATION)

    os.mkdir(DATASET_TEMPORARY_LOCATION)

    (x_train, y_train), (x_test, y_test) = batch_data_iter(TRAINING_SET_SIZE, TEST_SET_SIZE)

    torch.save(x_test, f"{DATASET_TEMPORARY_LOCATION}/x_test.pt")
    torch.save(y_test, f"{DATASET_TEMPORARY_LOCATION}/y_test.pt")

    print("Sketching dataset using K-means")

    for target_size in REDUCTIONS:
        print(f"K-means approximation step with '{target_size}' function...")

        start = time.time()
        x_train_km, y_train_km = compress(x_train, y_train, target_size)
        sketching_time[target_size] = time.time() - start

        os.mkdir(f"{DATASET_TEMPORARY_LOCATION}/{target_size}")

        torch.save(x_train_km, f"{DATASET_TEMPORARY_LOCATION}/{target_size}/x_train_km.pt")
        torch.save(y_train_km, f"{DATASET_TEMPORARY_LOCATION}/{target_size}/y_train_km.pt")

    shutil.move(DATASET_TEMPORARY_LOCATION, DATASET_LOCATION)
    json.dump(sketching_time, open(f"{RESULTS_LOCATION}/sketching-time.json", "w"), indent=4)

    print(f"Results saved in {DATASET_LOCATION}")


def run_tests():
    """
    Run the kernel perceptron implementation on the MNIST dataset
    using the sketched data-points, measure training time, test error and training error.
    """

    x_test = torch.load(f"{DATASET_LOCATION}/x_test.pt", map_location=DEVICE)
    y_test = torch.load(f"{DATASET_LOCATION}/y_test.pt", map_location=DEVICE)

    print(f"Running Multi-label Kernel Perceptron with k-means sketching on MNIST dataset")

    for reduction in REDUCTIONS:
        x_train_km = torch.load(f"{DATASET_LOCATION}/{reduction}/x_train_km.pt", map_location=DEVICE)
        y_train_km = torch.load(f"{DATASET_LOCATION}/{reduction}/y_train_km.pt", map_location=DEVICE)

        results = RESULTS_TEMPLATE.copy()
        epochs_iteration = tqdm(EPOCHS)

        for epochs in epochs_iteration:
            for degree in DEGREES:
                epochs_iteration.set_description(f"Training with {epochs} epoch(s) and degree {degree}")
                perceptron = MultilabelKernelPerceptron(
                    partial(polynomial, degree=degree),
                    label_set,
                    epochs,
                    x_train_km,
                    y_train_km,
                    DEVICE
                )

                training_time = time.time()
                perceptron.fit()
                training_time = time.time() - training_time

                results["epochs"][epochs]["degree"][degree] = {
                    "training_time": training_time,
                    "training_error": perceptron.error(x_train_km, y_train_km),
                    "test_error": perceptron.error(x_test, y_test)
                }

        save_to_csv(results, f"{RESULTS_LOCATION}/{reduction}-kmmkp.csv")


if __name__ == "__main__":
    compress_dataset()
    run_tests()
