from .notification_cache import (
    get_latest_notifications,
    get_unread_notification_count,
)


def app_notifications(request):
    if not request.user.is_authenticated:
        return {
            'navbar_notifications': [],
            'unread_notification_count': 0,
        }

    return {
        'navbar_notifications': get_latest_notifications(request.user.pk),
        'unread_notification_count': get_unread_notification_count(request.user.pk),
    }
