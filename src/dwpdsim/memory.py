"""DRAM cache manager with query-level processing."""

from dwpdsim.config import TierConfig
from dwpdsim.errors import InvalidPolicyDecisionError
from dwpdsim.interfaces import AggregateMetricsSink, BlockStorage
from dwpdsim.models import (
    AccessContext,
    BlockAccessResult,
    BlockId,
    CapacitySnapshot,
    MemAccessResult,
    MemAdmissionResult,
    MemQueryResult,
    Query,
)
from dwpdsim.policies.base import AdmissionPolicy, MemCachePolicy
from dwpdsim.policies.cache import LRUPolicy
from dwpdsim.policies.memory import AlwaysAdmitPolicy


class MemManager:
    """Owns DRAM state and processes complete queries in block order."""

    def __init__(
        self,
        config: TierConfig,
        lower_storage: BlockStorage,
        mem_cache_policy: MemCachePolicy | None = None,
        admission_policy: AdmissionPolicy | None = None,
    ) -> None:
        self._config = config
        self._lower_storage = lower_storage
        self._cache_policy = mem_cache_policy if mem_cache_policy is not None else LRUPolicy()
        self._admission_policy = (
            admission_policy if admission_policy is not None else AlwaysAdmitPolicy()
        )
        self._blocks: set[BlockId] = set()
        self._peak_used_blocks = 0
        self._eviction_count = 0

    @property
    def capacity(self) -> CapacitySnapshot:
        return CapacitySnapshot(
            capacity_blocks=self._config.capacity_blocks,
            used_blocks=len(self._blocks),
        )

    @property
    def peak_used_blocks(self) -> int:
        return self._peak_used_blocks

    @property
    def eviction_count(self) -> int:
        return self._eviction_count

    @property
    def resident_blocks(self) -> frozenset[BlockId]:
        return frozenset(self._blocks)

    def process_query(self, query: Query) -> MemQueryResult:
        """Process a query and retain detailed results for every block."""

        block_results: list[BlockAccessResult] = []
        for block_index in range(len(query.block_ids)):
            context = AccessContext.from_query(query, block_index)
            memory_result = self.access_block(context)
            if memory_result.hit:
                block_results.append(
                    BlockAccessResult(
                        block_id=context.block_id,
                        memory=memory_result,
                    )
                )
                continue

            if not self._lower_storage.contains_block(context.block_id):
                admission_result = self.admit_block(context)
                block_results.append(
                    BlockAccessResult(
                        block_id=context.block_id,
                        memory=memory_result,
                        admission=admission_result,
                        inserted_on_storage_miss=True,
                    )
                )
                continue

            storage_result = self._lower_storage.load_block(context)
            if self._admission_policy.should_admit(context, storage_result):
                admission_result = self.admit_block(context)
            else:
                admission_result = MemAdmissionResult(
                    block_id=context.block_id,
                    admitted=False,
                )

            block_results.append(
                BlockAccessResult(
                    block_id=context.block_id,
                    memory=memory_result,
                    storage=storage_result,
                    admission=admission_result,
                )
            )

        return MemQueryResult(query=query, block_results=tuple(block_results))

    def process_query_into(self, query: Query, sink: AggregateMetricsSink) -> None:
        """Process a query while streaming outcomes into an aggregate sink."""

        sink.record_query_start(query)
        for block_index in range(len(query.block_ids)):
            context = AccessContext.from_query(query, block_index)
            if self._access_is_hit(context):
                sink.record_memory_hit()
                continue

            if not self._lower_storage.contains_block(context.block_id):
                admission_result = self.admit_block(context)
                sink.record_memory_miss(
                    None,
                    admission_result,
                    inserted_on_storage_miss=True,
                )
                continue

            storage_result = self._lower_storage.load_block(context)
            if self._admission_policy.should_admit(context, storage_result):
                admission_result = self.admit_block(context)
            else:
                admission_result = MemAdmissionResult(
                    block_id=context.block_id,
                    admitted=False,
                )
            sink.record_memory_miss(storage_result, admission_result)

    def access_block(self, context: AccessContext) -> MemAccessResult:
        """Look up one block and update replacement metadata on a hit."""

        is_hit = self._access_is_hit(context)
        return MemAccessResult(block_id=context.block_id, hit=is_hit)

    def admit_block(self, context: AccessContext) -> MemAdmissionResult:
        """Insert one block, evicting a policy-selected victim if needed."""

        if context.block_id in self._blocks:
            self._cache_policy.on_hit(context)
            return MemAdmissionResult(block_id=context.block_id, admitted=False)

        evicted_block = None
        writeback_result = None
        if self.capacity.is_full:
            evicted_block = self._cache_policy.choose_victim(context)
            self._validate_victim(evicted_block)
            should_write_back = self._cache_policy.on_remove(evicted_block, context)
            if not isinstance(should_write_back, bool):
                raise InvalidPolicyDecisionError("MemCachePolicy.on_remove() must return bool")
            if should_write_back:
                writeback_context = AccessContext(
                    block_id=evicted_block,
                    timestamp=context.timestamp,
                    ttl=context.ttl,
                    query=context.query,
                )
                writeback_result = self._lower_storage.write_block(writeback_context)
            self._blocks.remove(evicted_block)
            self._eviction_count += 1

        self._blocks.add(context.block_id)
        self._cache_policy.on_insert(context)
        self._peak_used_blocks = max(self._peak_used_blocks, len(self._blocks))
        return MemAdmissionResult(
            block_id=context.block_id,
            admitted=True,
            evicted_block=evicted_block,
            writeback=writeback_result,
        )

    def _validate_victim(self, block_id: BlockId) -> None:
        if block_id not in self._blocks:
            raise InvalidPolicyDecisionError(
                f"MemCachePolicy selected non-resident DRAM block: {block_id}"
            )

    def _access_is_hit(self, context: AccessContext) -> bool:
        is_hit = context.block_id in self._blocks
        if is_hit:
            self._cache_policy.on_hit(context)
        return is_hit
