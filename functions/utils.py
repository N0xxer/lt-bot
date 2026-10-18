import disnake


def format_number(number: int) -> str:
    """Форматирует числа с разделителями (например, 1000000 -> 1 000 000)."""
    return f"{number:,}".replace(",", " ")


def is_staff_member(member: disnake.Member) -> bool:
    """
    Универсальная проверка: обладает ли участник сервера правами
    модератора или администратора.
    """
    perms = member.guild_permissions
    return (
        perms.administrator
        or perms.manage_guild
        or perms.manage_roles
        or perms.kick_members
        or perms.ban_members
    )