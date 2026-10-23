"""Workstation deployment access only; never included in the function image."""
import oci

config = oci.config.from_file()

def client(cls, region=None):
    return cls(dict(config, region=region or config['region']), timeout=(10, 30),
               retry_strategy=oci.retry.NoneRetryStrategy())

def all_pages(method, *args, **kwargs):
    return oci.pagination.list_call_get_all_results(method, *args, **kwargs).data
