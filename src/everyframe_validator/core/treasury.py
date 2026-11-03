"""Public treasury identity shipped with the official mainnet validator release."""
from .chain_scope import scope


TREASURY_WALLET_COLDKEY = '5CcGQCrAR5ktztYHRcj5izk5jmxSVdxbTdTxAva428iC83Qp'


def is_release_treasury(domain, coldkey):
    """Never inherit the mainnet recipient on a different chain or subnet."""
    return (coldkey == TREASURY_WALLET_COLDKEY
            and all(type(domain.get(k)) is type(v) and domain.get(k) == v
                    for k, v in scope('finney', 117).items()))
