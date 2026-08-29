from imbue.minds.desktop_client.assist_chat import build_assist_chat_message


def test_the_description_rides_the_assist_slash_command_verbatim() -> None:
    assert build_assist_chat_message("the database migration failed") == "/assist the database migration failed"
