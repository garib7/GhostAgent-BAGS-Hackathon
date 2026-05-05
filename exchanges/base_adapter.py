class BaseExchangeAdapter:
    def __init__(self, api_key: str, api_secret: str, testnet: bool = True):
        self.api_key = api_key
        self.api_secret = api_secret
        self.testnet = testnet

    async def get_market_price(self, symbol: str) -> float:
        raise NotImplementedError

    async def execute_trade(self, symbol: str, side: str, amount: float, price: float = None):
        raise NotImplementedError
