"""Objects of the store, each read with the store identity the caller names, so a read shows what
that identity may do."""

from wdf_clusterkit import store_credentials
from wdf_storecheck.s3 import client


def fetch(uri: str, identity: str, endpoint: str) -> bytes:
    """The object at `s3://<bucket>/<key>`, read from the store at `endpoint` and signed as
    `identity`, a role of contracts/store/roles.yaml whose credential the state directory holds."""
    bucket, _, key = uri.removeprefix("s3://").partition("/")
    if not uri.startswith("s3://") or not bucket or not key:
        raise ValueError(f"{uri} is not an s3://<bucket>/<key> uri")
    s3 = client(endpoint, store_credentials()[identity])
    return s3.get_object(Bucket=bucket, Key=key)["Body"].read()
