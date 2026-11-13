"""Shared offline spawn API for CLI, Ansible module_utils and future frontends."""
from .data import SpawnError, canonical, decode, digest
from .catalog import load_catalog
from .planner import make_plan, require_bound
from .schema import instance, template
from .sources import load_source

__all__ = ['SpawnError', 'canonical', 'decode', 'digest', 'load_catalog', 'make_plan', 'require_bound',
           'instance', 'template', 'load_source']

from .lifecycle import Lifecycle, LifecycleAdapter
from .identity import FileIdentityProvider, IdentityProvider
from .seed import SeedImporter
from .schema import name_record, read_name_record

__all__ += ["Lifecycle", "LifecycleAdapter", "FileIdentityProvider", "IdentityProvider",
            "SeedImporter", "name_record", "read_name_record"]

from .zulip import ZulipIdentityProvider
from .providers import AIAccessProvider, DirectKeyProvider, InferenceGateProvider, FileSecretProvider

__all__ += ['ZulipIdentityProvider', 'AIAccessProvider', 'DirectKeyProvider',
            'InferenceGateProvider', 'FileSecretProvider']
