import io
import os
import urllib.request
import warnings
from concurrent.futures.thread import ThreadPoolExecutor
from typing import Dict, List, Optional
from urllib.request import Request

import tqdm
from PIL import Image

from lightly.api import download, utils
from lightly.api.bitmask import BitMask
from lightly.openapi_generated.swagger_client.models import (
    DatasetEmbeddingData,
    ImageType,
)
from lightly.utils.hipify import bcolors


def _make_dir_and_save_image(output_dir: str, filename: str, img: Image):
    """Saves the images and creates necessary subdirectories."""
    path = os.path.join(output_dir, filename)

    head = os.path.split(path)[0]
    if not os.path.exists(head):
        os.makedirs(head)

    img.save(path)
    img.close()


def _get_image_from_read_url(read_url: str):
    """Makes a get request to the signed read url and returns the image."""
    request = Request(read_url, method="GET")
    with urllib.request.urlopen(request) as response:
        blob = response.read()
        img = Image.open(io.BytesIO(blob))
    return img


class _DownloadDatasetMixin:
    def download_dataset(
        self,
        output_dir: str,
        tag_name: str = "initial-tag",
        max_workers: int = 8,
        verbose: bool = True,
    ) -> None:
        """Downloads images from the web-app and stores them in output_dir.

        Args:
            output_dir:
                Where to store the downloaded images.
            tag_name:
                Name of the tag which should be downloaded.
            max_workers:
                Maximum number of workers downloading images in parallel.
            verbose:
                Whether or not to show the progress bar.

        Raises:
            ValueError:
                If the specified tag does not exist on the dataset.
            RuntimeError:
                If the connection to the server failed.

        Examples:
            >>> client = ApiWorkflowClient(token="MY_AWESOME_TOKEN")
            >>>
            >>> # Already created some Lightly Worker runs with this dataset
            >>> client.set_dataset_id_by_name("my-dataset")
            >>> client.download_dataset("/tmp/data")
            Downloading 3 images (with 3 workers):
            100%|██████████████████████████████████| 3/3 [00:01<00:00,  1.99imgs/s]
        """

        # check if images are available
        dataset = self._datasets_api.get_dataset_by_id(self.dataset_id)
        if dataset.img_type != ImageType.FULL:
            # only thumbnails or metadata available
            raise ValueError(
                f"Dataset with id {self.dataset_id} has no downloadable images!"
            )

        # check if tag exists
        available_tags = self.get_all_tags()
        try:
            tag = next(tag for tag in available_tags if tag.name == tag_name)
        except StopIteration:
            raise ValueError(
                f"Dataset with id {self.dataset_id} has no tag {tag_name}!"
            )

        # get sample ids
        sample_ids = list(
            utils.paginate_endpoint(
                self._mappings_api.get_sample_mappings_by_dataset_id,
                page_size=25000,
                dataset_id=self.dataset_id,
                field="_id",
            )
        )

        indices = BitMask.from_hex(tag.bit_mask_data).to_indices()
        sample_ids = [sample_ids[i] for i in indices]

        filenames_on_server = self.get_filenames()
        filenames = [filenames_on_server[i] for i in indices]

        downloadables = zip(sample_ids, filenames)

        # handle the case where len(sample_ids) < max_workers
        max_workers = min(len(sample_ids), max_workers)
        max_workers = max(max_workers, 1)

        if verbose:
            print(
                f"Downloading {bcolors.OKGREEN}{len(sample_ids)}{bcolors.ENDC} images (with {bcolors.OKGREEN}{max_workers}{bcolors.ENDC} workers):",
                flush=True,
            )
            pbar = tqdm.tqdm(unit="imgs", total=len(sample_ids))
            tqdm_lock = tqdm.tqdm.get_lock()

        # define lambda function for concurrent download
        def lambda_(i):
            sample_id, filename = i
            # try to download image
            try:
                read_url = self._samples_api.get_sample_image_read_url_by_id(
                    dataset_id=self.dataset_id,
                    sample_id=sample_id,
                    type="full",
                )
                img = _get_image_from_read_url(read_url)
                _make_dir_and_save_image(output_dir, filename, img)
                success = True
            except Exception as e:  # pylint: disable=broad-except
                warnings.warn(f"Downloading of image {filename} failed with error {e}")
                success = False

            # update the progress bar
            if verbose:
                tqdm_lock.acquire()
                pbar.update(1)
                tqdm_lock.release()
            # return whether the download was successful
            return success

        with ThreadPoolExecutor(max_workers=max_workers) as executor:
            results = list(executor.map(lambda_, downloadables, chunksize=1))

        if not all(results):
            msg = "Warning: Unsuccessful download! "
            msg += "Failed at image: {}".format(results.index(False))
            warnings.warn(msg)

    def get_all_embedding_data(self) -> List[DatasetEmbeddingData]:
        """Fetches embedding data of all embeddings for this dataset.

        Returns:
            A list of embedding data.

        Examples:
            >>> client = ApiWorkflowClient(token="MY_AWESOME_TOKEN")
            >>>
            >>> # Already created some Lightly Worker runs with this dataset
            >>> client.set_dataset_id_by_name("my-dataset")
            >>> client.get_all_embedding_data()
            [{'created_at': 1684750552181,
             'id': '646b40d88355e2f54c6d2235',
             'is2d': False,
             'is_processed': True,
             'name': 'default_20230522_10h15m50s'}]
        """
        return self._embeddings_api.get_embeddings_by_dataset_id(
            dataset_id=self.dataset_id
        )

    def get_embedding_data_by_name(self, name: str) -> DatasetEmbeddingData:
        """Fetches embedding data with the given name for this dataset.

        Args:
            name: Embedding name.

        Returns:
            Embedding data.

        Raises:
            ValueError:
                If no embedding with this name exists.

        Examples:
            >>> client = ApiWorkflowClient(token="MY_AWESOME_TOKEN")
            >>>
            >>> # Already created some Lightly Worker runs with this dataset
            >>> client.set_dataset_id_by_name("my-dataset")
            >>> client.get_embedding_data_by_name("embedding-data")
            [{'created_at': 1654756552401,
             'id': '646f346004d77b4e1424e67e',
             'is2d': False,
             'is_processed': True,
             'name': 'embedding-data'}]
        """
        for embedding_data in self.get_all_embedding_data():
            if embedding_data.name == name:
                return embedding_data
        raise ValueError(
            f"There are no embeddings with name '{name}' for dataset with id "
            f"'{self.dataset_id}'."
        )

    def download_embeddings_csv_by_id(
        self,
        embedding_id: str,
        output_path: str,
    ) -> None:
        """Downloads embeddings with the given embedding id from the dataset.

        Args:
            embedding_id: ID of the embedding data to be downloaded.
            output_path: Where the downloaded embedding data should be stored.

        Examples:
            >>> client = ApiWorkflowClient(token="MY_AWESOME_TOKEN")
            >>>
            >>> # Already created some Lightly Worker runs with this dataset
            >>> client.set_dataset_id_by_name("my-dataset")
            >>> client.download_embeddings_csv_by_id(
            ...     embedding_id="646f346004d77b4e1424e67e",
            ...     output_path="/tmp/embeddings.csv"
            ... )
            >>>
            >>> # File content:
            >>> # filenames,embedding_0,embedding_1,embedding_...,labels
            >>> # image-1.png,0.2124302,-0.26934767,...,0
        """
        read_url = self._embeddings_api.get_embeddings_csv_read_url_by_id(
            dataset_id=self.dataset_id, embedding_id=embedding_id
        )
        download.download_and_write_file(url=read_url, output_path=output_path)

    def download_embeddings_csv(self, output_path: str) -> None:
        """Downloads the latest embeddings from the dataset.

        Args:
            output_path: Where the downloaded embedding data should be stored.

        Raises:
            RuntimeError:
                If no embeddings could be found for the dataset.

        Examples:
            >>> client = ApiWorkflowClient(token="MY_AWESOME_TOKEN")
            >>>
            >>> # Already created some Lightly Worker runs with this dataset
            >>> client.set_dataset_id_by_name("my-dataset")
            >>> client.download_embeddings_csv(output_path="/tmp/embeddings.csv")
            >>>
            >>> # File content:
            >>> # filenames,embedding_0,embedding_1,embedding_...,labels
            >>> # image-1.png,0.2124302,-0.26934767,...,0
        """
        last_embedding = _get_latest_default_embedding_data(
            embeddings=self.get_all_embedding_data()
        )
        if last_embedding is None:
            raise RuntimeError(
                f"Could not find embeddings for dataset with id '{self.dataset_id}'."
            )
        self.download_embeddings_csv_by_id(
            embedding_id=last_embedding.id,
            output_path=output_path,
        )

    def export_label_studio_tasks_by_tag_id(
        self,
        tag_id: str,
    ) -> List[Dict]:
        """Exports samples in a format compatible with Label Studio.

        The format is documented here:
        https://labelstud.io/guide/tasks.html#Basic-Label-Studio-JSON-format

        Args:
            tag_id:
                Id of the tag which should exported.

        Returns:
            A list of dictionaries in a format compatible with Label Studio.

        Examples:
            >>> client = ApiWorkflowClient(token="MY_AWESOME_TOKEN")
            >>>
            >>> # Already created some Lightly Worker runs with this dataset
            >>> client.set_dataset_id_by_name("my-dataset")
            >>> client.export_label_studio_tasks_by_tag_id(tag_id="646f34608a5613b57d8b73cc")
            [{'id': 0, 'data': {'image': '...', ...}}]
        """
        label_studio_tasks = list(
            utils.paginate_endpoint(
                self._tags_api.export_tag_to_label_studio_tasks,
                page_size=2500,
                dataset_id=self.dataset_id,
                tag_id=tag_id,
            )
        )
        return label_studio_tasks


def _get_latest_default_embedding_data(
    embeddings: List[DatasetEmbeddingData],
) -> Optional[DatasetEmbeddingData]:
    """Returns the latest embedding data with a default name or None if no such
    default embedding exists.
    """
    default_embeddings = [e for e in embeddings if e.name.startswith("default")]
    if default_embeddings:
        last_embedding = sorted(default_embeddings, key=lambda e: e.created_at)[-1]
        return last_embedding
    else:
        return None
