import os
from logging import getLogger
from typing import Any, Dict, List, Optional

from django.conf import settings
from django.core.exceptions import ValidationError
from django.core.files.storage import default_storage
from django.db import models
from django.db.models import JSONField, Q
from django.utils.translation import gettext_lazy as _

from botocore.exceptions import ClientError
from web3._utils.normalizers import normalize_abi
from web3.contract import Contract

from gnosis.eth.clients import Sourcify
from gnosis.eth.django.models import EthereumAddressField
from gnosis.eth.ethereum_client import EthereumClientProvider, EthereumNetwork

from .clients import EtherscanClient
from .clients.etherscan_client import EtherscanClientConfigurationError

logger = getLogger(__name__)


def get_file_storage():
    if settings.AWS_CONFIGURED:
        from django_s3_storage.storage import S3Storage
        return S3Storage()
    else:
        return default_storage


def validate_abi(value: Dict[str, Any]):
    try:
        if not value:
            raise ValueError('Empty ABI not allowed')
        normalize_abi(value)
    except ValueError as exc:
        raise ValidationError(
            _('%(value)s is not a valid Ethereum Contract ABI: %(reason)s'),
            params={'value': value, 'reason': str(exc)},
        )


class ContractAbi(models.Model):
    """
    This model holds contract ABIs. Contract ABIS don't have to be tied to a contract
    (e.g. generic ERC20/721 ABI)
    """
    abi = JSONField(validators=[validate_abi])
    description = models.CharField(max_length=200, blank=True)
    relevance = models.SmallIntegerField(default=100)  # A lower number will indicate more relevance

    def __str__(self):
        return f'ContractABI {self.relevance} - {self.description}'

    def clean(self):
        try:
            contract_abi = ContractAbi.objects.exclude(pk=self.pk).get(abi=self.abi)
            raise ValidationError(_(f'Abi cannot be duplicated. Already exists: '
                                    f'{contract_abi.pk} - {contract_abi.description}'))
        except ContractAbi.DoesNotExist:
            pass

    def abi_functions(self) -> List[str]:
        return [x['name'] for x in self.abi if x['type'] == 'function']


def get_contract_logo_path(instance: 'Contract', filename):
    # file will be uploaded to MEDIA_ROOT/<address>
    _, extension = os.path.splitext(filename)
    return f'contracts/logos/{instance.address}{extension}'  # extension includes '.'


class ContractManager(models.Manager):
    def create_from_address(self, address: str, network_id: int = 1) -> Optional[Contract]:
        sourcify = Sourcify(EthereumNetwork(network_id))
        try:
            contract_metadata = sourcify.get_contract_metadata(address)
        except IOError:
            return
        if contract_metadata:
            if contract_metadata.abi:
                contract_abi, _ = ContractAbi.objects.update_or_create(abi=contract_metadata.abi,
                                                                       defaults={
                                                                           'description': contract_metadata.name,
                                                                       })
            else:
                contract_abi = None
            return super().create(
                address=address,
                name=contract_metadata.name,
                contract_abi=contract_abi,
            )
        else:  # Fallback to etherscan API (no name for contract)
            try:
                etherscan = EtherscanClient(EthereumNetwork(network_id), api_key=settings.ETHERSCAN_API_KEY)
                abi = etherscan.get_contract_abi(address)
                if abi:
                    try:
                        contract_abi = ContractAbi.objects.get(abi=abi)
                    except ContractAbi.DoesNotExist:
                        contract_abi = ContractAbi.objects.create(abi=abi, description='')
                    return super().create(
                        address=address,
                        name='',
                        contract_abi=contract_abi,
                    )
            except EtherscanClientConfigurationError:
                return

    def fix_missing_logos(self) -> int:
        """
        Syncs contracts with empty logos with files that exist on S3 and match the address. This usually happens
        when logos
        :return: Number of synced logos
        """
        synced_logos = 0
        for contract in self.without_logo():
            filename = get_contract_logo_path(contract, f'{contract.address}.png')
            contract.logo.name = filename
            try:
                if contract.logo.size:
                    synced_logos += 1
                    contract.save(update_fields=['logo'])
                    logger.info('Found logo on url %s', contract.logo.url)
            except (ClientError, FileNotFoundError):  # Depending on aws or filesystem
                logger.error('Error retrieving url %s', contract.logo.url)
        return synced_logos


class ContractQuerySet(models.QuerySet):
    no_logo_query = Q(logo=None) | Q(logo='')

    def with_logo(self):
        return self.exclude(self.no_logo_query)

    def without_logo(self):
        return self.filter(self.no_logo_query)


class Contract(models.Model):
    objects = ContractManager.from_queryset(ContractQuerySet)()
    address = EthereumAddressField(primary_key=True)
    name = models.CharField(max_length=200, blank=True, default='')
    display_name = models.CharField(max_length=200, blank=True, default='')
    logo = models.ImageField(blank=True, default='',
                             upload_to=get_contract_logo_path, storage=get_file_storage)
    contract_abi = models.ForeignKey(ContractAbi, on_delete=models.SET_NULL, null=True, default=None, blank=True,
                                     related_name='contracts')

    def __str__(self):
        has_abi = self.contract_abi_id is not None
        logo = ' with logo' if self.logo else ' without logo'
        return f'Contract {self.address} - {self.name} - with abi {has_abi}{logo}'

    def get_main_name(self):
        """
        :return: `display_name` if available, else use scraped `name`
        """
        return self.display_name if self.display_name else self.name

    def sync_abi_from_api(self, network: Optional[EthereumNetwork] = None) -> bool:
        """
        Sync ABI from Sourcify, then from EtherScan
        :param network: Can be provided to save requests to the node
        :return: True if updated, False otherwise
        """
        ethereum_client = EthereumClientProvider()
        network = network or ethereum_client.get_network()
        sourcify = Sourcify(EthereumNetwork(network))
        contract_abi = None
        try:
            contract_metadata = sourcify.get_contract_metadata(self.address)
            if contract_metadata:
                contract_abi, _ = ContractAbi.objects.update_or_create(
                    abi=contract_metadata.abi,
                    defaults={'description': contract_metadata.name}
                )
        except IOError:
            pass

        if not contract_abi:
            etherscan_api = EtherscanClient(network)
            try:
                if abi := etherscan_api.get_contract_abi(self.address):
                    contract_abi, _ = ContractAbi.objects.update_or_create(abi=abi)
            except IOError:
                pass

        if contract_abi:
            self.contract_abi = contract_abi
            self.save(update_fields=['contract_abi'])

        return bool(contract_abi)
