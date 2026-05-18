from aiogram import Router

from .captcha import captcha_router
from .chat import chat_router
from .logs import logs_router

router = Router()
router.include_router(captcha_router)
router.include_router(chat_router)
router.include_router(logs_router)
