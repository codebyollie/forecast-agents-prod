from types import SimpleNamespace

from forecast_ai.proof.publisher import transaction_fee_fields


class _Eip1559Eth:
    gas_price = 27_306_000
    max_priority_fee = 1_000_000

    @staticmethod
    def get_block(block_identifier):
        assert block_identifier == "pending"
        return {"baseFeePerGas": 28_444_000}


class _LegacyEth:
    gas_price = 20_000_000

    @staticmethod
    def get_block(block_identifier):
        assert block_identifier == "pending"
        return {}


def test_eip1559_fees_stay_above_a_rising_base_fee():
    fees = transaction_fee_fields(SimpleNamespace(eth=_Eip1559Eth()))

    assert fees == {
        "maxPriorityFeePerGas": 1_000_000,
        "maxFeePerGas": 57_888_000,
    }
    assert fees["maxFeePerGas"] > _Eip1559Eth.get_block("pending")["baseFeePerGas"]


def test_legacy_fee_uses_a_safety_margin():
    fees = transaction_fee_fields(SimpleNamespace(eth=_LegacyEth()))

    assert fees == {"gasPrice": 25_000_000}
