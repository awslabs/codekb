import json
import logging
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed

import boto3
from botocore.config import Config
from typing import Optional, Dict, Any, List
from ..core.config import get_config, require_config
from ..core.logging_utils import format_payload, StorageError
from ..core.prompt_spotlighting import neutralize

logger = logging.getLogger(__name__)

# S3 Vectors caps a query's metadata filter near 10 KB, so only ~200 space ids
# fit in one $in. A larger scope is split into chunks of this size and merged.
_MAX_SCOPE_IDS_PER_QUERY = 200
# How many chunk queries to run at once in the scatter-gather path.
_SCOPE_QUERY_WORKERS = 8


def _log_aws_call(api: str, request: Dict[str, Any], response: Any = None) -> None:
    """Emit a full-payload DEBUG line for an AWS call."""
    if not logger.isEnabledFor(logging.DEBUG):
        return
    logger.debug("AWS %s request:\n%s", api, format_payload(request))
    if response is not None:
        logger.debug("AWS %s response:\n%s", api, format_payload(response))


class S3VectorsClient:
    _bedrock_client = None
    # Guards lazy creation of the shared class-level Bedrock client below.
    # Concurrent boto3.client() creation on the default session is not
    # thread-safe; reusing an already-built client across threads is fine.
    _bedrock_client_lock = threading.Lock()

    @staticmethod
    def _pool_size() -> int:
        """HTTP connection-pool size, sized to the summarization concurrency.

        botocore defaults max_pool_connections to 10. Under parallel
        summarization, embedding/index calls funnel through these clients too,
        so size the pool to the configured concurrency to avoid capping real
        parallelism at ~10 and churning connections.
        """
        try:
            concurrency = int(require_config("summarization.concurrency") or 1)
        except (TypeError, ValueError):
            concurrency = 1
        return max(concurrency, 10)

    def __init__(self, region: Optional[str] = None):
        s3_config = get_config("storage", {}).get("s3_vectors", {})
        region = region or s3_config.get("region") or require_config("storage.s3.region")

        self.bucket_name = s3_config.get("bucket_name")
        if not self.bucket_name:
            raise StorageError(
                "S3 Vectors bucket is not configured. Set storage.s3_vectors.bucket_name "
                "via the CODEKB_S3_VECTORS_BUCKET environment variable to a "
                "globally-unique bucket you own."
            )

        self.s3vectors = boto3.client(
            "s3vectors",
            region_name=region,
            config=Config(max_pool_connections=self._pool_size()),
        )
        self.index_name = s3_config.get("index_name")
        self._region = region
        self._model_id = s3_config.get("embedding_model", "amazon.titan-embed-text-v2:0")
        self._dimension = s3_config.get("dimension", 1024)
        logger.debug(
            f"S3VectorsClient initialized: bucket={self.bucket_name}, index={self.index_name}, region={region}"
        )

    def _generate_embedding(self, text: str) -> List[float]:
        """Generate vector embedding using Amazon Titan Text Embeddings V2 via Bedrock."""
        import time

        with S3VectorsClient._bedrock_client_lock:
            if S3VectorsClient._bedrock_client is None:
                S3VectorsClient._bedrock_client = boto3.client(
                    "bedrock-runtime",
                    region_name=self._region,
                    config=Config(max_pool_connections=self._pool_size()),
                )
                logger.info(
                    f"Bedrock runtime client initialized for embedding model '{self._model_id}'"
                )

        body = json.dumps(
            {
                "inputText": text,
                "dimensions": self._dimension,
            }
        )
        logger.debug(
            f"invoke_model: model={self._model_id}, text_len={len(text)}, dimensions={self._dimension}"
        )
        _log_aws_call(
            "bedrock.invoke_model",
            {
                "modelId": self._model_id,
                "contentType": "application/json",
                "accept": "application/json",
                "body": {"inputText": text, "dimensions": self._dimension},
            },
        )
        t0 = time.perf_counter()
        response = S3VectorsClient._bedrock_client.invoke_model(
            modelId=self._model_id,
            contentType="application/json",
            accept="application/json",
            body=body,
        )
        elapsed_ms = (time.perf_counter() - t0) * 1000
        result = json.loads(response["body"].read())
        embedding = result["embedding"]
        _log_aws_call(
            "bedrock.invoke_model",
            request={"modelId": self._model_id},  # compact — request already logged above
            response={
                **{k: v for k, v in result.items() if k != "embedding"},
                "embedding_len": len(embedding),
                "embedding_preview": embedding[:4],
            },
        )
        logger.debug(f"invoke_model returned {len(embedding)}-dim embedding in {elapsed_ms:.1f}ms")
        return embedding

    def insert_document(
        self, document_id: str, content: str, metadata: Optional[Dict[str, Any]] = None
    ) -> Dict[str, Any]:
        """Insert a document with vector embedding."""
        logger.debug(f"insert_document: id={document_id}, content_len={len(content)}")

        # Untrusted repository text lands here on its way into the shared index,
        # where it resurfaces to other users and to the agent. Strip spotlighting
        # delimiter-lookalikes at this write boundary — the same scheme the LLM
        # call sites wrap with — so injected content cannot persist a forged
        # marker into stored descriptions or the embedded vector.
        content = neutralize(content)
        embedding = self._generate_embedding(content)
        sanitized_metadata = self._sanitize_metadata(self._neutralize_metadata(metadata or {}))

        vector_data = {
            "key": document_id,
            "data": {"float32": embedding},
            "metadata": sanitized_metadata,
        }

        put_request = {
            "vectorBucketName": self.bucket_name,
            "indexName": self.index_name,
            "vectors": [
                {
                    "key": document_id,
                    "data": {"float32_len": len(embedding), "float32_preview": embedding[:4]},
                    "metadata": sanitized_metadata,
                }
            ],
        }
        _log_aws_call("s3vectors.put_vectors", put_request)
        try:
            response = self.s3vectors.put_vectors(
                vectorBucketName=self.bucket_name, indexName=self.index_name, vectors=[vector_data]
            )
            _log_aws_call("s3vectors.put_vectors", request={"id": document_id}, response=response)
        except self.s3vectors.exceptions.NotFoundException:
            raise RuntimeError(
                f"S3 Vectors index not found: bucket='{self.bucket_name}', index='{self.index_name}'. "
            )
        except Exception as e:
            logger.error(f"put_vectors failed for '{document_id}': {e}", exc_info=True)
            raise

        logger.debug(f"insert_document succeeded: id={document_id}")
        return response

    @staticmethod
    def _neutralize_metadata(metadata: Dict[str, Any]) -> Dict[str, Any]:
        """Strip spotlighting delimiter-lookalikes from untrusted string metadata.

        Metadata carries repo-derived free text (notably ``description``, plus
        names and paths) that resurfaces on query. Neutralise string values —
        including strings nested in lists — so a forged marker cannot persist
        into the shared index. Non-string values are left untouched.
        """

        def _clean(value: Any) -> Any:
            if isinstance(value, str):
                return neutralize(value)
            if isinstance(value, (list, tuple, set)):
                return [_clean(item) for item in value]
            return value

        return {key: _clean(value) for key, value in metadata.items()}

    @staticmethod
    def _sanitize_metadata(metadata: Dict[str, Any]) -> Dict[str, Any]:
        """Remove metadata values rejected by S3 Vectors.

        S3 Vectors rejects empty arrays in metadata. Drop empty containers and
        null values at the write boundary so callers can pass natural Python
        metadata without knowing every service validation edge.
        """
        sanitized: Dict[str, Any] = {}
        for key, value in metadata.items():
            if value is None:
                continue
            if isinstance(value, (list, tuple, set)):
                cleaned_values = [item for item in value if item is not None]
                if not cleaned_values:
                    continue
                sanitized[key] = cleaned_values
                continue
            if isinstance(value, dict):
                cleaned_dict = {
                    nested_key: nested_value
                    for nested_key, nested_value in value.items()
                    if nested_value is not None
                    and not (
                        isinstance(nested_value, (list, tuple, set, dict))
                        and len(nested_value) == 0
                    )
                }
                if not cleaned_dict:
                    continue
                sanitized[key] = cleaned_dict
                continue
            sanitized[key] = value
        return sanitized

    def _resolve_top_k(self, top_k: Optional[int]) -> int:
        """Fall back to the configured default when the caller didn't set top_k."""
        return top_k if top_k is not None else require_config("storage.s3_vectors.default_top_k")

    def _query_vectors(
        self,
        query_embedding: List[float],
        top_k: int,
        filter_criteria: Optional[Dict[str, Any]] = None,
    ) -> List[Dict[str, Any]]:
        """One ANN query for a prebuilt embedding.

        ``filter_criteria=None`` searches the whole index. Factored out so the
        scoped (scatter-gather) path can reuse a single embedding across chunks.
        """
        import time

        kwargs: Dict[str, Any] = {
            "vectorBucketName": self.bucket_name,
            "indexName": self.index_name,
            "queryVector": {"float32": query_embedding},
            "topK": top_k,
            "returnDistance": True,
            "returnMetadata": True,
        }
        if filter_criteria is not None:
            kwargs["filter"] = filter_criteria

        logger.debug(
            f"query_vectors: bucket={self.bucket_name}, index={self.index_name}, "
            f"top_k={top_k}, filter={filter_criteria if filter_criteria is not None else '<none>'}"
        )
        _log_aws_call(
            "s3vectors.query_vectors",
            {
                "vectorBucketName": self.bucket_name,
                "indexName": self.index_name,
                "queryVector": {
                    "float32_len": len(query_embedding),
                    "float32_preview": query_embedding[:4],
                },
                "topK": top_k,
                **({"filter": filter_criteria} if filter_criteria is not None else {}),
                "returnDistance": True,
                "returnMetadata": True,
            },
        )
        t0 = time.perf_counter()
        try:
            response = self.s3vectors.query_vectors(**kwargs)
            _log_aws_call("s3vectors.query_vectors", request={"topK": top_k}, response=response)
        except Exception as e:
            logger.error(f"query_vectors failed (filter={filter_criteria}): {e}", exc_info=True)
            raise

        elapsed_ms = (time.perf_counter() - t0) * 1000
        results = response.get("vectors", [])
        logger.debug(
            f"query_vectors returned {len(results)} result(s) in {elapsed_ms:.1f}ms (top_k={top_k})"
        )
        return results

    def retrieve_documents(
        self, query_text: str, top_k: Optional[int] = None
    ) -> List[Dict[str, Any]]:
        """Retrieve documents by semantic similarity across the whole index."""
        top_k = self._resolve_top_k(top_k)
        return self._query_vectors(self._generate_embedding(query_text), top_k)

    def retrieve_with_metadata_filter(
        self, query_text: str, filter_criteria: Dict[str, Any], top_k: Optional[int] = None
    ) -> List[Dict[str, Any]]:
        """Retrieve documents with metadata filtering."""
        top_k = self._resolve_top_k(top_k)
        return self._query_vectors(self._generate_embedding(query_text), top_k, filter_criteria)

    def retrieve_scoped(
        self, query_text: str, space_ids: List[str], top_k: Optional[int] = None
    ) -> List[Dict[str, Any]]:
        """Semantic search restricted to an explicit, non-empty set of space ids.

        One $in filter can't hold more than ~200 ids (the 10 KB filter cap), so a
        larger scope is split into chunks, the same query embedding is run against
        each (scatter-gather), and the chunk results are merged by distance down to
        top_k. The index is ANN, so this is the normal approximate ranking —
        gathered across chunks, not a weaker approximation (each chunk searches a
        smaller set). Chunks run concurrently; one chunk's failure fails the call.
        """
        if not space_ids:
            raise ValueError("retrieve_scoped requires a non-empty space_ids list")
        top_k = self._resolve_top_k(top_k)
        embedding = self._generate_embedding(query_text)
        chunks = [
            space_ids[i : i + _MAX_SCOPE_IDS_PER_QUERY]
            for i in range(0, len(space_ids), _MAX_SCOPE_IDS_PER_QUERY)
        ]
        if len(chunks) == 1:
            return self._query_vectors(embedding, top_k, {"space_id": {"$in": chunks[0]}})

        merged: List[Dict[str, Any]] = []
        with ThreadPoolExecutor(max_workers=min(len(chunks), _SCOPE_QUERY_WORKERS)) as pool:
            futures = [
                pool.submit(self._query_vectors, embedding, top_k, {"space_id": {"$in": chunk}})
                for chunk in chunks
            ]
            for fut in as_completed(futures):
                merged.extend(fut.result())  # a chunk error propagates and fails the search
        # Nearest first; key breaks ties so the order is stable across runs.
        merged.sort(key=lambda r: (r.get("distance", float("inf")), str(r.get("key", ""))))
        return merged[:top_k]

    def get_vector(self, key: str) -> Optional[Dict[str, Any]]:
        """Get a specific vector by key."""
        logger.debug(
            f"s3vectors.get_vectors: bucket={self.bucket_name}, index={self.index_name}, key={key}"
        )
        try:
            response = self.s3vectors.get_vectors(
                vectorBucketName=self.bucket_name,
                indexName=self.index_name,
                keys=[key],
                returnMetadata=True,
            )
            vectors = response.get("vectors", [])
            logger.debug(f"s3vectors.get_vectors ok: key={key}, hit={bool(vectors)}")
            return vectors[0] if vectors else None
        except Exception as e:
            logger.error(f"get_vector failed for key '{key}': {e}", exc_info=True)
            raise

    def get_descriptions(self, keys: List[str]) -> Dict[str, str]:
        """Stored ``description`` metadata per key. Keys with no vector are omitted."""
        unique_keys = list(dict.fromkeys(keys))
        descriptions: Dict[str, str] = {}
        for start in range(0, len(unique_keys), self._GET_BATCH_SIZE):
            batch = unique_keys[start : start + self._GET_BATCH_SIZE]
            response = self.s3vectors.get_vectors(
                vectorBucketName=self.bucket_name,
                indexName=self.index_name,
                keys=batch,
                returnMetadata=True,
            )
            for vector in response.get("vectors", []):
                description = (vector.get("metadata") or {}).get("description")
                if description:
                    descriptions[vector["key"]] = description
        logger.debug(
            f"s3vectors.get_vectors: {len(descriptions)}/{len(unique_keys)} key(s) had a description"
        )
        return descriptions

    def list_vectors(self, max_results: int = 100) -> List[Dict[str, Any]]:
        """List all vectors in the index with metadata."""
        logger.debug(
            f"s3vectors.list_vectors: bucket={self.bucket_name}, index={self.index_name}, "
            f"max_results={max_results}"
        )
        try:
            response = self.s3vectors.list_vectors(
                vectorBucketName=self.bucket_name,
                indexName=self.index_name,
                maxResults=max_results,
                returnMetadata=True,
            )
            vectors = response.get("vectors", [])
            logger.debug(f"s3vectors.list_vectors returned {len(vectors)} result(s)")
            return vectors
        except Exception as e:
            logger.error(f"list_vectors failed: {e}", exc_info=True)
            raise

    def count_vectors(self, segment_count: int = 4) -> int:
        """Count all vectors in the index using parallel segment listing."""
        import concurrent.futures
        import time

        def count_segment(segment_index: int) -> int:
            count = 0
            next_token = None
            pages = 0
            while True:
                kwargs = {
                    "vectorBucketName": self.bucket_name,
                    "indexName": self.index_name,
                    "maxResults": 1000,
                    "segmentCount": segment_count,
                    "segmentIndex": segment_index,
                    "returnData": False,
                    "returnMetadata": False,
                }
                if next_token:
                    kwargs["nextToken"] = next_token
                response = self.s3vectors.list_vectors(**kwargs)
                count += len(response.get("vectors", []))
                pages += 1
                next_token = response.get("nextToken")
                if not next_token:
                    break
            logger.debug(
                f"s3vectors.count_vectors segment {segment_index}/{segment_count}: "
                f"{count} vector(s) across {pages} page(s)"
            )
            return count

        logger.debug(
            f"s3vectors.count_vectors: bucket={self.bucket_name}, index={self.index_name}, "
            f"segments={segment_count}"
        )
        t0 = time.perf_counter()
        with concurrent.futures.ThreadPoolExecutor(max_workers=segment_count) as executor:
            futures = [executor.submit(count_segment, i) for i in range(segment_count)]
            total = sum(f.result() for f in futures)
        elapsed_ms = (time.perf_counter() - t0) * 1000
        logger.debug(
            f"s3vectors.count_vectors total={total} in {elapsed_ms:.0f}ms across "
            f"{segment_count} segment(s)"
        )
        return total

    def delete_vector(self, key: str) -> Dict[str, Any]:
        """Delete a specific vector by key."""
        logger.debug(
            f"s3vectors.delete_vectors: bucket={self.bucket_name}, index={self.index_name}, "
            f"keys=[{key}]"
        )
        try:
            response = self.s3vectors.delete_vectors(
                vectorBucketName=self.bucket_name, indexName=self.index_name, keys=[key]
            )
            logger.debug(f"Deleted vector: {key}")
            return response
        except Exception as e:
            logger.error(f"delete_vector failed for key '{key}': {e}", exc_info=True)
            raise

    def delete_vectors_by_keys(self, keys: List[str]) -> int:
        """Delete the given vector keys in batches. Returns the number submitted."""
        if not keys:
            return 0

        unique_keys = list(dict.fromkeys(keys))
        total_submitted = 0
        for start in range(0, len(unique_keys), self._DELETE_BATCH_SIZE):
            batch = unique_keys[start : start + self._DELETE_BATCH_SIZE]
            total_submitted += self._flush_delete_batch(batch)

        if total_submitted:
            logger.info(
                f"Submitted {total_submitted} key(s) for deletion from index '{self.index_name}'"
            )
        return total_submitted

    # ── internal helpers ────────────────────────────────────────────────

    # S3 Vectors DeleteVectors API accepts at most 500 keys per call.
    _DELETE_BATCH_SIZE = 500
    # S3 Vectors GetVectors API accepts at most 100 keys per call.
    _GET_BATCH_SIZE = 100

    def _flush_delete_batch(self, keys: List[str]) -> int:
        """Delete a single batch of up to 500 keys."""
        self.s3vectors.delete_vectors(
            vectorBucketName=self.bucket_name,
            indexName=self.index_name,
            keys=keys,
        )
        return len(keys)
