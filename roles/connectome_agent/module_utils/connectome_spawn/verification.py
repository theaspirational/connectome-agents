"""Evidence predicates shared by the serviceReady gate and staged verification."""


def proven(receipt, key):
    return (type(receipt) is dict and receipt.get('state') == 'applied' and
            type(receipt.get('evidence')) is dict and bool(receipt['evidence']) and
            type(receipt.get('result')) is dict and receipt['result'].get(key) is True)
