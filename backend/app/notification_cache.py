from django.core.cache import cache
from django.db import transaction


NOTIFICATION_CACHE_TIMEOUT = 60


def _latest_key(user_id):
    return f'notifications:{user_id}:latest'


def _unread_count_key(user_id):
    return f'notifications:{user_id}:unread-count'


def invalidate_notification_cache(user_id):
    if user_id is None:
        return

    keys = [_latest_key(user_id), _unread_count_key(user_id)]
    transaction.on_commit(lambda: cache.delete_many(keys))


def get_latest_notifications(user_id):
    from .models import AppNotification

    key = _latest_key(user_id)
    notifications = cache.get(key)
    if notifications is None:
        notifications = list(
            AppNotification.objects.filter(recipient_id=user_id)
            .order_by('-created_at', '-pk')[:6]
        )
        cache.set(key, notifications, NOTIFICATION_CACHE_TIMEOUT)
    return notifications


def get_unread_notification_count(user_id):
    from .models import AppNotification

    key = _unread_count_key(user_id)
    count = cache.get(key)
    if count is None:
        count = AppNotification.objects.filter(
            recipient_id=user_id,
            read_at__isnull=True,
        ).count()
        cache.set(key, count, NOTIFICATION_CACHE_TIMEOUT)
    return count
