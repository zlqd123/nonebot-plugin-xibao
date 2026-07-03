from nonebot import on_command, require
from nonebot.plugin import PluginMetadata, inherit_supported_adapters

require("nonebot_plugin_saa")

import io
from pathlib import Path

import nonebot_plugin_saa as saa
from nonebot.adapters import Message
from nonebot.params import CommandArg
from PIL import Image, ImageDraw, ImageFont
from pilmoji import Pilmoji
from pilmoji.source import GoogleEmojiSource

import string

__plugin_meta__ = PluginMetadata(
    name="喜（悲）报生成器",
    description="生成喜报（或是悲报，管他呢）",
    usage="/喜报 [文字] 生成喜报\n/悲报 [文字] 生成悲报",
    type="application",
    homepage="https://github.com/sp2dev/nonebot-plugin-xibao",
    supported_adapters=inherit_supported_adapters("nonebot_plugin_saa"),
)

# 插件内置字体文件路径。
# 使用固定字体可以减少不同系统下渲染结果差异（尤其是中文字符宽度差异）。
font_path = Path(__file__).parent / "SourceHanSansSC-Regular.otf"


# 安全边距（按底图宽高比例计算）：
# 1. 限制文本绘制区域，避免贴边。
# 2. 避开模板背景中的主要视觉元素（例如边框、装饰图形、角色等）。
# 3. 不同分辨率下保持相对一致的视觉留白。
SAFE_MARGIN_TOP = 0.18
SAFE_MARGIN_BOTTOM = 0.08
SAFE_MARGIN_LEFT = 0.10
SAFE_MARGIN_RIGHT = 0.07

# 自动字号与排版参数。
# DEFAULT_FONT_SIZE：默认起始字号。
# WRAP_TRIGGER_FONT_SIZE：字号降到该值以下时启用自动换行。
# MIN_FONT_SIZE：继续缩小时的下限。
# LINE_HEIGHT_FACTOR：行高 = 字号 * 因子。
DEFAULT_FONT_SIZE = 160
WRAP_TRIGGER_FONT_SIZE = 80
MIN_FONT_SIZE = 30
LINE_HEIGHT_FACTOR = 1
STROKE_WIDTH = 10

# 手动上下偏移（像素）：
# 在自动垂直居中的基础上再做一次整体偏移。
# 正数整体下移，负数整体上移，可用于快速微调视觉中心。
TEXT_VERTICAL_OFFSET_PX = -100

# 需要跳过的标点符号（全角 + 半角）
SKIP_PUNCTUATION = (
    # 半角标点
    string.punctuation +  # !"#$%&'()*+,-./:;<=>?@[\]^_`{|}~
    # 全角标点（中文常用）
    "，。、；：！？"  # 全角逗号、句号、顿号、分号、冒号、感叹号、问号
    "（）【】《》「」『』"  # 各类括号
    "——……"  # 破折号、省略号
    "～"  # 波浪号
    "·"  # 间隔号
)

def strip_leading_punctuation(text: str) -> str:
    """移除开头的标点符号（全角/半角），保留后面的内容"""
    if not text:
        return text
    
    # 去除开头空白
    text = text.lstrip()
    
    # 循环移除开头的标点符号
    while text and text[0] in SKIP_PUNCTUATION:
        text = text[1:].lstrip()
    
    return text


def _get_safe_rect(image_width: int, image_height: int) -> tuple[int, int, int, int]:
    """根据图片尺寸计算文本可用安全区域。

    返回值是 (left, top, right, bottom)，用于后续统一的排版与居中计算。
    """
    left = int(image_width * SAFE_MARGIN_LEFT)
    right = int(image_width * (1 - SAFE_MARGIN_RIGHT))
    top = int(image_height * SAFE_MARGIN_TOP)
    bottom = int(image_height * (1 - SAFE_MARGIN_BOTTOM))
    return left, top, right, bottom


def _load_font(font_size: int) -> ImageFont.FreeTypeFont:
    return ImageFont.truetype(font_path, font_size)


def _measure_text(
    text: str,
    draw: ImageDraw.ImageDraw,
    pilmoji: Pilmoji,
    font: ImageFont.FreeTypeFont,
    stroke_width: int,
) -> tuple[int, int]:
    """测量单行文本宽高，优先使用 Pilmoji 以兼容 emoji。

    说明：
    - Pilmoji 负责将 emoji 按图片方式渲染，普通 PIL 测量在某些情况下会低估尺寸。
    - 若 Pilmoji 不支持 getsize（或调用失败），回退到 ImageDraw.textbbox。
    - 返回结果会把描边厚度纳入尺寸，避免居中计算时出现“看起来偏移”。
    """
    if not text:
        return 0, int(font.size * LINE_HEIGHT_FACTOR)

    try:
        if hasattr(pilmoji, "getsize"):
            width, height = pilmoji.getsize(text, font=font)
            return int(width) + stroke_width * 2, int(height) + stroke_width * 2
    except (AttributeError, TypeError, ValueError):
        pass

    bbox = draw.textbbox((0, 0), text, font=font, stroke_width=stroke_width)
    return int(bbox[2] - bbox[0]), int(bbox[3] - bbox[1])


def _estimate_font_size(text: str, available_width: int) -> int:
    """根据文本长度给出一个线性缩小的初始字号。

    这个结果不是最终值，只用于让后续的精确拟合更快收敛。
    """
    visible_text = text.replace("\n", "")
    if not visible_text:
        return DEFAULT_FONT_SIZE

    approximate_char_width = max(1, int(DEFAULT_FONT_SIZE * 0.1))
    single_line_capacity = max(1, available_width // approximate_char_width)
    shrink_ratio = min(1.0, len(visible_text) / single_line_capacity)
    estimated = round(
            DEFAULT_FONT_SIZE
            - (DEFAULT_FONT_SIZE - WRAP_TRIGGER_FONT_SIZE) * shrink_ratio
        )
    return max(WRAP_TRIGGER_FONT_SIZE, min(DEFAULT_FONT_SIZE, estimated))


def _line_height(font_size: int) -> int:
    return max(1, int(font_size * LINE_HEIGHT_FACTOR))


def _layout_lines(
    text: str,
    draw: ImageDraw.ImageDraw,
    pilmoji: Pilmoji,
    font: ImageFont.FreeTypeFont,
    max_width: int,
    wrap: bool,
) -> list[str]:
    if not text:
        return []

    lines: list[str] = []
    for paragraph in text.split("\n"):
        if wrap:
            wrapped_lines = _wrap_text_by_width(
                paragraph, draw, pilmoji, font, max_width
            )
            lines.extend(wrapped_lines or [""])
        else:
            lines.append(paragraph)

    return lines


def _layout_fits(
    lines: list[str],
    draw: ImageDraw.ImageDraw,
    pilmoji: Pilmoji,
    font: ImageFont.FreeTypeFont,
    max_width: int,
    max_height: int,
) -> bool:
    if not lines:
        return True

    total_height = _line_height(round(font.size)) * max(1, len(lines))
    if total_height > max_height:
        return False

    for line in lines:
        line_width, _ = _measure_text(line, draw, pilmoji, font, STROKE_WIDTH)
        if line_width > max_width:
            return False

    return True


def _wrap_text_by_width(
    text: str,
    draw: ImageDraw.ImageDraw,
    pilmoji: Pilmoji,
    font: ImageFont.FreeTypeFont,
    max_width: int,
) -> list[str]:
    """按最大宽度逐字换行，保留原始段落换行。
    """
    if not text:
        return []

    lines: list[str] = []
    for paragraph in text.splitlines() or [text]:
        if not paragraph:
            lines.append("")
            continue
        # 逐段处理：用户手动输入的换行会被保留为段落边界。
        current = ""
        for char in paragraph:
            candidate = current + char
            candidate_width, _ = _measure_text(
                candidate, draw, pilmoji, font, STROKE_WIDTH
            )
            if candidate_width <= max_width or not current:
                current = candidate
            else:
                # 超出宽度后，将上一段落行落地，再从当前字符重新开始。
                lines.append(current)
                current = char

        if current:
            lines.append(current)

    return lines


def _calculate_font_size(
    text: str,
    image_width: int,
    image_height: int,
    max_font_size: int = DEFAULT_FONT_SIZE,
    min_font_size: int = MIN_FONT_SIZE,
) -> tuple[int, list[str]]:
    """在安全区域内寻找最大可用字号。

    逻辑分两段：
    1. 先按线性估算值从大到小寻找单行排版的最大可用字号；
    2. 如果字号降到 80 以下仍然放不下，则切换为自动换行，再继续缩小。
    """
    if not text:
        return max_font_size, []

    left, top, right, bottom = _get_safe_rect(image_width, image_height)
    available_width = max(1, right - left)
    available_height = max(1, bottom - top)

    measure_img = Image.new("RGB", (1, 1))
    draw = ImageDraw.Draw(measure_img)

    with Pilmoji(measure_img) as pilmoji:
        estimated_size = min(
            max_font_size, _estimate_font_size(text, available_width)
        )

        for font_size in range(estimated_size, WRAP_TRIGGER_FONT_SIZE - 1, -1):
            font = _load_font(font_size)
            lines = _layout_lines(text, draw, pilmoji, font, available_width, False)
            if _layout_fits(
                lines,
                draw,
                pilmoji,
                font,
                available_width,
                available_height,
            ):
                return font_size, lines

        wrapped_start = min(WRAP_TRIGGER_FONT_SIZE, estimated_size)
        for font_size in range(wrapped_start, min_font_size - 1, -1):
            font = _load_font(font_size)
            lines = _layout_lines(text, draw, pilmoji, font, available_width, True)
            if _layout_fits(
                lines,
                draw,
                pilmoji,
                font,
                available_width,
                available_height,
            ):
                return font_size, lines

        fallback_font = _load_font(min_font_size)
        fallback_lines = _layout_lines(
            text, draw, pilmoji, fallback_font, available_width, True
        )

    return min_font_size, fallback_lines


async def _generate_image(
    bg_file: str,
    text: str = "",
    font_size: int | None = None,
    text_color: str = "black",
    stroke: str = "",
    vertical_offset_px: int = TEXT_VERTICAL_OFFSET_PX,
) -> bytes:
    """通用图片生成流程：加载底图、排版文本、绘制并导出 JPEG。

    参数：
    - bg_file: 背景图文件名（位于插件目录）。
    - text: 要绘制的文本；为空时仅输出底图。
    - font_size: 可选的字号上限，不传时使用全局最大字号。
    - text_color/stroke: 文本颜色和描边颜色。
    - vertical_offset_px: 手动上下偏移，用于微调视觉中心。
    """
    img_path = Path(__file__).parent / bg_file
    with Image.open(img_path) as opened_img:
        img = opened_img.convert("RGB")
        image_width, image_height = img.size

        if not text.strip():
            # 无文本时直接返回底图，避免不必要的测量与绘制开销。
            output = io.BytesIO()
            img.save(output, format="JPEG")
            return output.getvalue()

        left, top, right, bottom = _get_safe_rect(image_width, image_height)
        available_width = max(1, right - left)
        available_height = max(1, bottom - top)

        # font_size 被视为“上限”而非固定值，最终仍会自动收缩以适应可用区域。
        auto_max_font = font_size if font_size is not None else DEFAULT_FONT_SIZE
        resolved_font_size, lines = _calculate_font_size(
            text,
            image_width,
            image_height,
            max_font_size=max(MIN_FONT_SIZE, auto_max_font),
            min_font_size=MIN_FONT_SIZE,
        )

        draw = ImageDraw.Draw(img)
        font = _load_font(resolved_font_size)
        line_height = _line_height(resolved_font_size)
        total_height = line_height * max(1, len(lines))
        # 文字先在安全区域内垂直居中，再叠加手动上下偏移。
        # 这样默认居中效果稳定，且保留可配置“视觉微调旋钮”。
        start_y = top + (available_height - total_height) / 2 + vertical_offset_px

        with Pilmoji(img,source=GoogleEmojiSource) as pilmoji:
            for index, line in enumerate(lines):
                line_width, _ = _measure_text(line, draw, pilmoji, font, STROKE_WIDTH)
                # 每行单独水平居中，确保长短行混排时版心一致。
                x = left + (available_width - line_width) / 2
                y = start_y + index * line_height
                pilmoji.text(
                    (int(x), int(y)),
                    line,
                    spacing=0,
                    fill=text_color,
                    font=font,
                    stroke_fill=stroke,
                    stroke_width=STROKE_WIDTH,
                    emoji_position_offset=(5, 0),
                )

        output = io.BytesIO()
        img.save(output, format="JPEG")
        return output.getvalue()


async def gen_xibao(text: str = "", font_size: int | None = None) -> bytes:
    return await _generate_image(
        "xibao_bg.jpg", text, font_size, "red", stroke="yellow"
    )


async def gen_beibao(text: str = "", font_size: int | None = None) -> bytes:
    return await _generate_image(
        "beibao_bg.jpg", text, font_size, "black", stroke="white"
    )


genxibao = on_command(
    "喜报",
    aliases={
        "喜报：",
        "喜报:",
    },
)


@genxibao.handle()
async def xibaohandle(args: Message = CommandArg()) -> None:
    textinput = args.extract_plain_text()
    textinput = strip_leading_punctuation(textinput)
    picdata = await gen_xibao(text=textinput)
    await saa.Image(picdata).send()


genbeibao = on_command(
    "悲报",
    aliases={
        "悲报：",
        "悲报:",
    },
)


@genbeibao.handle()
async def beibaohandle(args: Message = CommandArg()) -> None:
    textinput = args.extract_plain_text()
    textinput = strip_leading_punctuation(textinput)
    picdata = await gen_beibao(text=textinput)
    await saa.Image(picdata).send()
