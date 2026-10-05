"""Призначення проксі новим акаунтам без мережевих запитів."""
from ..models import Proxy


def random_working_proxy():
    """Випадкова проксі з активних і позначених робочими; None, якщо пул порожній."""
    return Proxy.objects.filter(is_active=True, is_working=True).order_by("?").first()
