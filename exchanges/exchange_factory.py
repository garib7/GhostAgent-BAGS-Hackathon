from .base_adapter import BaseExchangeAdapter
from .solana_agent_adapter import SolanaAgentAdapter

class ExchangeFactory:
    @staticmethod
    def create_exchange(exchange_id: str, api_key: str, api_secret: str, testnet: bool = True):
        if exchange_id.lower() == "solana":
            return SolanaAgentAdapter(api_key, api_secret, testnet)
        else:
            raise ValueError(f"Exchange {exchange_id} is not supported in this hackathon branch.")
