from collections import defaultdict
from typing import TYPE_CHECKING, DefaultDict, Dict, Iterable, List

import graphene
from django.core.exceptions import ValidationError
from django.db import transaction

from ....core.permissions import ProductPermissions
from ....product.error_codes import ProductErrorCode
from ....product.models import ProductChannelListing, ProductVariantChannelListing
from ...channel import ChannelContext
from ...channel.types import Channel
from ...core.mutations import BaseMutation
from ...core.scalars import Decimal
from ...core.types.common import ProductChannelListingError
from ...core.utils import get_duplicated_values, get_duplicates_ids
from ...utils import resolve_global_ids_to_primary_keys
from ..types.products import Product, ProductVariant

if TYPE_CHECKING:
    from ....product.models import (
        Product as ProductModel,
        ProductVariant as ProductVariantModel,
    )
    from ....channel.models import Channel as ChannelModel

ErrorType = DefaultDict[str, List[ValidationError]]


class ProductChannelListingAddInput(graphene.InputObjectType):
    channel_id = graphene.ID(required=True, description="ID of a channel.")
    is_published = graphene.Boolean(
        description="Determines if product is visible to customers.", required=True
    )
    publication_date = graphene.types.datetime.Date(
        description="Publication date. ISO 8601 standard."
    )


class ProductChannelListingUpdateInput(graphene.InputObjectType):
    add_channels = graphene.List(
        graphene.NonNull(ProductChannelListingAddInput),
        description="List of channels to which the product should be assigned.",
        required=False,
    )
    remove_channels = graphene.List(
        graphene.NonNull(graphene.ID),
        description="List of channels from which the product should be unassigned.",
        required=False,
    )


class ProductChannelListingUpdate(BaseMutation):
    product = graphene.Field(Product, description="An updated product instance.")

    class Arguments:
        id = graphene.ID(required=True, description="ID of a product to update.")
        input = ProductChannelListingUpdateInput(
            required=True,
            description="Fields required to create product channel listings.",
        )

    class Meta:
        description = "Manage product's availability in channels."
        permissions = (ProductPermissions.MANAGE_PRODUCTS,)
        error_type_class = ProductChannelListingError
        error_type_field = "products_errors"

    @classmethod
    def validate_duplicated_ids(
        cls,
        add_channels_ids: Iterable[str],
        remove_channels_ids: Iterable[str],
        errors: ErrorType,
    ):
        duplicated_ids = get_duplicates_ids(add_channels_ids, remove_channels_ids)
        if duplicated_ids:
            error_msg = (
                "The same object cannot be in both lists "
                "for adding and removing items."
            )
            errors["input"].append(
                ValidationError(
                    error_msg,
                    code=ProductErrorCode.DUPLICATED_INPUT_ITEM.value,
                    params={"channels": list(duplicated_ids)},
                )
            )

    @classmethod
    def validate_duplicated_values(
        cls, channels_ids: Iterable[str], field_name: str, errors: ErrorType
    ):
        duplicates = get_duplicated_values(channels_ids)
        if duplicates:
            errors[field_name].append(
                ValidationError(
                    "Duplicated channel ID.",
                    code=ProductErrorCode.DUPLICATED_INPUT_ITEM.value,
                    params={"channels": duplicates},
                )
            )

    @classmethod
    def clean_channels(cls, info, input, errors: ErrorType) -> Dict:
        add_channels = input.get("add_channels", [])
        add_channels_ids = [channel["channel_id"] for channel in add_channels]
        remove_channels_ids = input.get("remove_channels", [])

        cls.validate_duplicated_ids(add_channels_ids, remove_channels_ids, errors)
        cls.validate_duplicated_values(add_channels_ids, "add_channels", errors)
        cls.validate_duplicated_values(remove_channels_ids, "remove_channels", errors)

        if errors:
            return {}
        channels_to_add: List["ChannelModel"] = []
        if add_channels_ids:
            channels_to_add = cls.get_nodes_or_error(
                add_channels_ids, "channel_id", Channel
            )
        _, remove_channels_pks = resolve_global_ids_to_primary_keys(
            remove_channels_ids, Channel
        )

        cleaned_input = {"add_channels": [], "remove_channels": remove_channels_pks}

        for channel_listing, channel in zip(add_channels, channels_to_add):
            channel_listing["channel"] = channel
            cleaned_input["add_channels"].append(channel_listing)

        return cleaned_input

    @classmethod
    def validate_product_without_category(cls, cleaned_input, errors: ErrorType):
        channels_with_published_product_without_category = []
        for add_channel in cleaned_input.get("add_channels", []):
            if add_channel.get("is_published") is True:
                channels_with_published_product_without_category.append(
                    add_channel["channel_id"]
                )
        if channels_with_published_product_without_category:
            errors["is_published"].append(
                ValidationError(
                    "You must select a category to be able to publish.",
                    code=ProductErrorCode.PRODUCT_WITHOUT_CATEGORY.value,
                    params={
                        "channels": channels_with_published_product_without_category
                    },
                )
            )

    @classmethod
    def add_channels(cls, product: "ProductModel", add_channels: List[Dict]):
        for add_channel in add_channels:
            defaults = {
                "is_published": add_channel.get("is_published"),
                "publication_date": add_channel.get("publication_date", None),
            }
            ProductChannelListing.objects.update_or_create(
                product=product, channel=add_channel["channel"], defaults=defaults
            )

    @classmethod
    def remove_channels(cls, product: "ProductModel", remove_channels: List[int]):
        ProductChannelListing.objects.filter(
            product=product, channel_id__in=remove_channels
        ).delete()

    @classmethod
    @transaction.atomic()
    def save(cls, info, product: "ProductModel", cleaned_input: Dict):
        cls.add_channels(product, cleaned_input.get("add_channels", []))
        cls.remove_channels(product, cleaned_input.get("remove_channels", []))

    @classmethod
    def perform_mutation(cls, _root, info, id, input):
        product = cls.get_node_or_error(info, id, only_type=Product, field="id")
        errors = defaultdict(list)

        cleaned_input = cls.clean_channels(info, input, errors)
        if not product.category:
            cls.validate_product_without_category(cleaned_input, errors)
        if errors:
            raise ValidationError(errors)

        cls.save(info, product, cleaned_input)
        return ProductChannelListingUpdate(
            product=ChannelContext(node=product, channel_slug=None)
        )


class ProductVariantChannelListingAddInput(graphene.InputObjectType):
    channel_id = graphene.ID(required=True, description="ID of a channel.")
    price = Decimal(
        required=True, description="Price of the particular variant in channel."
    )


# TODO: Use BaseChannelListingMutation after rebase.
class ProductVaraintChannelListingUpdate(BaseMutation):
    variant = graphene.Field(
        ProductVariant, description="An updated product variant instance."
    )

    class Arguments:
        id = graphene.ID(
            required=True, description="ID of a product variant to update."
        )
        input = graphene.List(
            graphene.NonNull(ProductVariantChannelListingAddInput),
            required=True,
            description=(
                "List of fields required to create product variant channel listings."
            ),
        )

    class Meta:
        description = "Manage product varaint prices in channels."
        permissions = (ProductPermissions.MANAGE_PRODUCTS,)
        error_type_class = ProductChannelListingError
        error_type_field = "products_errors"

    @classmethod
    def clean_channels(cls, info, input, errors: ErrorType) -> List:
        add_channels_ids = [
            channel_listing_data["channel_id"] for channel_listing_data in input
        ]
        cleaned_input = []

        duplicates = get_duplicated_values(add_channels_ids)
        if duplicates:
            errors["channelId"].append(
                ValidationError(
                    "Duplicated channel ID.",
                    code=ProductErrorCode.DUPLICATED_INPUT_ITEM.value,
                    params={"channels": duplicates},
                )
            )
        else:
            channels: List["ChannelModel"] = []
            if add_channels_ids:
                channels = cls.get_nodes_or_error(
                    add_channels_ids, "channel_id", Channel
                )
            for channel_listing_data, channel in zip(input, channels):
                channel_listing_data["channel"] = channel
                cleaned_input.append(channel_listing_data)
        return cleaned_input

    @classmethod
    def validate_product_assigned_to_channel(
        cls, variant: "ProductVariantModel", cleaned_input: List, errors: ErrorType
    ):
        channel_pks = [
            channel_listing_data["channel"].pk for channel_listing_data in cleaned_input
        ]
        channels_assigned_to_product = list(
            ProductChannelListing.objects.filter(
                product=variant.product_id
            ).values_list("channel_id", flat=True)
        )
        channels_not_assigned_to_product = set(channel_pks) - set(
            channels_assigned_to_product
        )
        if channels_not_assigned_to_product:
            channel_global_ids = []
            for channel_listing_data in cleaned_input:
                if (
                    channel_listing_data["channel"].pk
                    in channels_not_assigned_to_product
                ):
                    channel_global_ids.append(channel_listing_data["channel_id"])
            errors["input"].append(
                ValidationError(
                    "Product not available in channels.",
                    code=ProductErrorCode.PRODUCT_NOT_ASSIGNED_TO_CHANNEL.value,
                    params={"channels": channel_global_ids},
                )
            )

    @classmethod
    def clean_prices(cls, info, cleaned_input, errors: ErrorType) -> List:
        channels_with_invalid_price = []
        for channel_listing_data in cleaned_input:
            price = channel_listing_data.get("price")
            if price is not None and price < 0:
                channels_with_invalid_price.append(channel_listing_data["channel_id"])
        if channels_with_invalid_price:
            errors["price"].append(
                ValidationError(
                    "Product price cannot be lower than 0.",
                    code=ProductErrorCode.INVALID.value,
                    params={"channels": channels_with_invalid_price},
                )
            )
        return cleaned_input

    @classmethod
    @transaction.atomic()
    def save(cls, info, variant: "ProductVariantModel", cleaned_input: List):
        for channel_listing_data in cleaned_input:
            channel = channel_listing_data["channel"]
            defaults = {
                "price_amount": channel_listing_data.get("price"),
                "currency": channel.currency_code,
            }
            ProductVariantChannelListing.objects.update_or_create(
                variant=variant, channel=channel, defaults=defaults,
            )

    @classmethod
    def perform_mutation(cls, _root, info, id, input):
        variant: "ProductVariantModel" = cls.get_node_or_error(  # type: ignore
            info, id, only_type=ProductVariant, field="id"
        )
        errors = defaultdict(list)

        cleaned_input = cls.clean_channels(info, input, errors)
        cls.validate_product_assigned_to_channel(variant, cleaned_input, errors)
        cleaned_input = cls.clean_prices(info, cleaned_input, errors)

        if errors:
            raise ValidationError(errors)

        cls.save(info, variant, cleaned_input)

        return ProductVaraintChannelListingUpdate(
            variant=ChannelContext(node=variant, channel_slug=None)
        )
