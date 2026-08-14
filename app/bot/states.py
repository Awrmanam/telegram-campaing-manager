from aiogram.fsm.state import State, StatesGroup


class CampaignWizard(StatesGroup):
    name = State()
    sender = State()
    interval = State()
    custom_interval = State()
    targets = State()
    message = State()
    preview = State()


class TextInput(StatesGroup):
    campaign_rename = State()
    campaign_interval = State()
    message_add = State()
    message_edit = State()
    test_message = State()
    test_preview = State()
