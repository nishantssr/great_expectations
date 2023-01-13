from __future__ import annotations

import dataclasses
import logging
from pprint import pformat as pf
from typing import (
    TYPE_CHECKING,
    Any,
    Dict,
    Generic,
    List,
    MutableMapping,
    Optional,
    Set,
    Type,
    TypeVar,
    Union,
)

import pydantic
from pydantic import Field
from pydantic import dataclasses as pydantic_dc
from pydantic import root_validator
from typing_extensions import ClassVar, TypeAlias, TypeGuard

from great_expectations.core.id_dict import BatchKwargs, BatchSpec
from great_expectations.experimental.datasources.experimental_base_model import (
    ExperimentalBaseModel,
)
from great_expectations.experimental.datasources.metadatasource import MetaDatasource
from great_expectations.experimental.datasources.sources import _SourceFactories
from great_expectations.types import SerializableDictDot
from great_expectations.validator.metric_configuration import MetricConfiguration

LOGGER = logging.getLogger(__name__)

if TYPE_CHECKING:
    import pandas as pd

    from great_expectations.execution_engine import ExecutionEngine
    from great_expectations.validator.computed_metric import MetricValue

# BatchRequestOptions is a dict that is composed into a BatchRequest that specifies the
# Batches one wants returned. The keys represent dimensions one can slice the data along
# and the values are the realized. If a value is None or unspecified, the batch_request
# will capture all data along this dimension. For example, if we have a year and month
# splitter and we want to query all months in the year 2020, the batch request options
# would look like:
#   options = { "year": 2020 }
BatchRequestOptions: TypeAlias = Dict[str, Any]


@dataclasses.dataclass(frozen=True)
class BatchRequest:
    datasource_name: str
    data_asset_name: str
    options: BatchRequestOptions


class BatchRequestError(Exception):
    pass


@pydantic_dc.dataclass(frozen=True)
class BatchSorter:
    key: str
    reverse: bool = False


BatchSortersDefinition: TypeAlias = Union[List[BatchSorter], List[str]]


def _is_batch_sorter_list(
    sorters: BatchSortersDefinition,
) -> TypeGuard[list[BatchSorter]]:
    if len(sorters) == 0 or isinstance(sorters[0], BatchSorter):
        return True
    return False


def _is_str_sorter_list(sorters: BatchSortersDefinition) -> TypeGuard[list[str]]:
    if len(sorters) > 0 and isinstance(sorters[0], str):
        return True
    return False


def _batch_sorter_from_list(sorters: BatchSortersDefinition) -> List[BatchSorter]:
    if _is_batch_sorter_list(sorters):
        return sorters
    # mypy doesn't successfully type-narrow sorters to a List[str] here so we use
    # another TypeGuard. We could cast instead which may be slightly faster.
    if _is_str_sorter_list(sorters):
        return [_batch_sorter_from_str(sorter) for sorter in sorters]
    # This should never be reached because of static typing but is necessary because
    # mypy doesn't know of the if conditions must evaluate to True.
    raise ValueError(
        f"sorters is a not a BatchSortersDefinition but is a {type(sorters)}"
    )


def _batch_sorter_from_str(sort_key: str) -> BatchSorter:
    """Convert a list of strings to BatchSorters

    Args:
        sort_key: A batch metadata key which will be used to sort batches on a data asset.
                  This can be prefixed with a + or - to indicate increasing or decreasing
                  sorting. If not specified, defaults to increasing order.
    """
    if sort_key[0] == "-":
        return BatchSorter(key=sort_key[1:], reverse=True)
    elif sort_key[0] == "+":
        return BatchSorter(key=sort_key[1:], reverse=False)
    else:
        return BatchSorter(key=sort_key, reverse=False)


class DataAsset(ExperimentalBaseModel):
    # To subclass a DataAsset one must define `type` as a Class literal explicitly on the sublass
    # as well as implementing the methods in the `Abstract Methods` section below.
    # Some examples:
    # * type: Literal["MyAssetTypeID"] = "MyAssetTypeID",
    # * type: Literal["table"] = "table"
    # * type: Literal["csv"] = "csv"
    name: str
    type: str
    order_by: List[BatchSorter] = Field(default_factory=list)

    # non-field private attrs
    _datasource: Datasource = pydantic.PrivateAttr()

    @property
    def datasource(self) -> Datasource:
        return self._datasource

    # TODO (kilo): remove setter and add custom init for DataAsset to inject datasource in constructor??
    # This setter is non-functional: https://github.com/pydantic/pydantic/issues/3395
    # There is some related discussion linked from that ticket which may be a workaround.
    @datasource.setter
    def datasource(self, ds: Datasource):
        assert isinstance(ds, Datasource)
        self._datasource = ds

    # Abstract Methods
    def batch_request_options_template(
        self,
    ) -> BatchRequestOptions:
        """A BatchRequestOptions template for get_batch_request.

        Returns:
            A BatchRequestOptions dictionary with the correct shape that get_batch_request
            will understand. All the option values are defaulted to None.
        """
        raise NotImplementedError

    def get_batch_list_from_batch_request(
        self, batch_request: BatchRequest
    ) -> List[Batch]:
        raise NotImplementedError

    # End Abstract Methods

    def get_batch_request(
        self, options: Optional[BatchRequestOptions] = None
    ) -> BatchRequest:
        """A batch request that can be used to obtain batches for this DataAsset.

        Args:
            options: A dict that can be used to limit the number of batches returned from the asset.
                The dict structure depends on the asset type. A template of the dict can be obtained by
                calling batch_request_options_template.

        Returns:
            A BatchRequest object that can be used to obtain a batch list from a Datasource by calling the
            get_batch_list_from_batch_request method.
        """
        if options is not None and not self._valid_batch_request_options(options):
            raise BatchRequestError(
                "Batch request options should have a subset of keys:\n"
                f"{list(self.batch_request_options_template().keys())}\n"
                f"but actually has the form:\n{pf(options)}\n"
            )
        return BatchRequest(
            datasource_name=self._datasource.name,
            data_asset_name=self.name,
            options=options or {},
        )

    def _valid_batch_request_options(self, options: BatchRequestOptions) -> bool:
        return set(options.keys()).issubset(
            set(self.batch_request_options_template().keys())
        )

    def _validate_batch_request(self, batch_request: BatchRequest) -> None:
        """Validates the batch_request has the correct form.

        Args:
            batch_request: A batch request object to be validated.
        """
        if not (
            batch_request.datasource_name == self.datasource.name
            and batch_request.data_asset_name == self.name
            and self._valid_batch_request_options(batch_request.options)
        ):
            expect_batch_request_form = BatchRequest(
                datasource_name=self.datasource.name,
                data_asset_name=self.name,
                options=self.batch_request_options_template(),
            )
            raise BatchRequestError(
                "BatchRequest should have form:\n"
                f"{pf(dataclasses.asdict(expect_batch_request_form))}\n"
                f"but actually has form:\n{pf(dataclasses.asdict(batch_request))}\n"
            )

    # Sorter methods
    @pydantic.validator("order_by", pre=True, each_item=True)
    def _parse_order_by_sorter(
        cls, v: Union[str, BatchSorter]
    ) -> Union[BatchSorter, dict]:
        if isinstance(v, str):
            if not v:
                raise ValueError("empty string")
            return _batch_sorter_from_str(v)
        return v

    def add_sorters(
        self: DataAssetType, sorters: BatchSortersDefinition
    ) -> DataAssetType:
        # NOTE: (kilo59) we could use pydantic `validate_assignment` for this
        # https://docs.pydantic.dev/usage/model_config/#options
        self.order_by = _batch_sorter_from_list(sorters)
        return self

    def sort_batches(self, batch_list: List[Batch]) -> None:
        """Sorts batch_list in place in the order configured in this DataAsset.

        Args:
            batch_list: The list of batches to sort in place.
        """
        for sorter in reversed(self.order_by):
            try:
                batch_list.sort(
                    key=lambda b: b.metadata[sorter.key],
                    reverse=sorter.reverse,
                )
            except KeyError as e:
                raise KeyError(
                    f"Trying to sort {self.name} table asset batches on key {sorter.key} "
                    "which isn't available on all batches."
                ) from e


# If a Datasource can have more than 1 DataAssetType, this will need to change.
DataAssetType = TypeVar("DataAssetType", bound=DataAsset)


class Datasource(
    ExperimentalBaseModel, Generic[DataAssetType], metaclass=MetaDatasource
):
    # To subclass Datasource one needs to define:
    # asset_types
    # type
    # assets
    #
    # The important part of defining `assets` is setting the Dict type correctly.
    # In addition, one must define the methods in the `Abstract Methods` section below.
    # If one writes a class level docstring, this will become the documenation for the
    # data context method `data_context.sources.add_my_datasource` method.

    # class attrs
    asset_types: ClassVar[List[Type[DataAsset]]] = []
    # Datasource instance attrs but these will be fed into the `execution_engine` constructor
    _excluded_eng_args: ClassVar[Set[str]] = {
        "name",
        "type",
        "execution_engine",
        "assets",
    }
    # Setting this in a Datasource subclass will override the execution engine type.
    # The primary use case is to inject an execution engine for testing.
    execution_engine_override: ClassVar[Optional[Type[ExecutionEngine]]] = None

    # instance attrs
    type: str
    name: str
    assets: MutableMapping[str, DataAssetType] = {}
    _execution_engine: ExecutionEngine = pydantic.PrivateAttr()

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        engine_kwargs = {
            k: v for (k, v) in kwargs.items() if k not in self._excluded_eng_args
        }
        self._execution_engine = self._execution_engine_type()(**engine_kwargs)

    @property
    def execution_engine(self) -> ExecutionEngine:
        return self._execution_engine

    class Config:
        # TODO: revisit this (1 option - define __get_validator__ on ExecutionEngine)
        # https://pydantic-docs.helpmanual.io/usage/types/#custom-data-types
        arbitrary_types_allowed = True

    @pydantic.validator("assets", pre=True)
    @classmethod
    def _load_asset_subtype(cls, v: Dict[str, dict]):
        LOGGER.info(f"Loading 'assets' ->\n{pf(v, depth=3)}")
        loaded_assets: Dict[str, DataAssetType] = {}

        # TODO (kilo59): catch key errors
        for asset_name, config in v.items():
            asset_type_name: str = config["type"]
            asset_type: Type[DataAssetType] = _SourceFactories.type_lookup[
                asset_type_name
            ]
            LOGGER.debug(f"Instantiating '{asset_type_name}' as {asset_type}")
            loaded_assets[asset_name] = asset_type(**config)

        LOGGER.debug(f"Loaded 'assets' ->\n{repr(loaded_assets)}")
        return loaded_assets

    def _execution_engine_type(self) -> Type[ExecutionEngine]:
        """Returns the execution engine to be used"""
        return self.execution_engine_override or self.execution_engine_type

    def get_batch_list_from_batch_request(
        self, batch_request: BatchRequest
    ) -> List[Batch]:
        """A list of batches that correspond to the BatchRequest.

        Args:
            batch_request: A batch request for this asset. Usually obtained by calling
                get_batch_request on the asset.

        Returns:
            A list of batches that match the options specified in the batch request.
        """
        data_asset = self.get_asset(batch_request.data_asset_name)
        return data_asset.get_batch_list_from_batch_request(batch_request)

    def get_asset(self, asset_name: str) -> DataAssetType:
        """Returns the DataAsset referred to by name"""
        # This default implementation will be used if protocol is inherited
        try:
            return self.assets[asset_name]
        except KeyError as exc:
            raise LookupError(
                f"'{asset_name}' not found. Available assets are {list(self.assets.keys())}"
            ) from exc

    def add_asset(self, asset: DataAssetType) -> DataAssetType:
        """Adds an asset to a datasource

        Args:
            asset: The DataAsset to be added to this datasource.
        """
        # The setter for datasource is non-functional so we access _datasource directly.
        # See the comment in DataAsset for more information.
        asset._datasource = self
        self.assets[asset.name] = asset
        return asset

    # Abstract Methods
    @property
    def execution_engine_type(self) -> Type[ExecutionEngine]:
        """Return the ExecutionEngine type use for this Datasource"""
        raise NotImplementedError(
            "One needs to implement 'execution_engine_type' on a Datasource subclass"
        )

    # End Abstract Methods


class BatchError(Exception):
    pass


class Batch(ExperimentalBaseModel):
    """This represents a batch of data.

    This is usually not the data itself but a hook to the data on an external datastore such as
    a spark or a sql database. An exception exists for pandas or any in-memory datastore.
    """

    datasource: Datasource
    data_asset: DataAsset
    batch_request: BatchRequest
    data: Any  # Due to circular imports we can't use: PandasBatchData | SqlAlchemyBatchData | SparkDFBatchData
    id: str
    # metadata is any arbitrary data one wants to associate with a batch. GX will add arbitrary metadata
    # to a batch so developers may want to namespace any custom metadata they add.
    metadata: dict[str, Any] = {}

    # TODO: These legacy fields are currently required. They are only used in usage stats so we
    #       should figure out a better way to anonymize and delete them.
    legacy_batch_markers: BatchKwargs  # Due to circular imports we can't use BatchMarkers
    legacy_batch_spec: BatchSpec
    legacy_batch_definition: SerializableDictDot  # Due to circular imports we can't use BatchDefinition

    class Config:
        allow_mutation = False
        arbitrary_types_allowed = True
        extra = pydantic.Extra.forbid
        validate_assignment = True

    @root_validator(pre=True)
    def _set_id(cls, values: dict) -> dict:
        # We need to unique identifier. This will likely change as we get more input
        options_list = []
        for k, v in values["batch_request"].options.items():
            options_list.append(f"{k}_{v}")
        values["id"]: str = "-".join(
            [values["datasource"].name, values["data_asset"].name, *options_list]
        )
        return values

    def head(self, n_rows: int = 5) -> pd.DataFrame:
        if n_rows and n_rows > 0:
            self.data.execution_engine.batch_manager.load_batch_list(batch_list=[self])
            metric = MetricConfiguration(
                metric_name="table.head",
                metric_domain_kwargs={"batch_id": self.id},
                metric_value_kwargs={"n_rows": n_rows, "fetch_all": False},
            )
            resolved_metrics: dict[
                tuple[str, str, str], MetricValue
            ] = self.data.execution_engine.resolve_metrics(metrics_to_resolve=(metric,))
            return resolved_metrics[metric.id]
        else:
            raise BatchError(
                f"n_rows must be a positive integer, but {n_rows} was passed."
            )
