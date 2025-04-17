# 1744WOSBOT/cogs/leaderboard_ocr.py
from __future__ import annotations
import asyncio, discord, io
from discord.ext import commands
from discord import app_commands
from datetime import datetime
from enum import Enum
from typing import List, Tuple, Dict

from constants                import (
    OCR_DEBUG_LOG,
    PINNED_MESSAGES_FILE,
    DEFAULT_LEADERBOARD_CH,
)
from utils.text_utils         import clean_int
from utils.json_utils         import load_json, save_json
from services.db_service      import ProcessedImageDB, NicknameResolver
from services.ocr_service     import (
    ocr_lines_from_image,
    parse_damage_score,
    parse_tag_and_name,
)
from services.sheet_service   import SheetService

class EventType(str, Enum):
    BEAR_TRAP_1   = "Bear Trap 1"
    BEAR_TRAP_2   = "Bear Trap 2"
    CRAZY_JOE     = "Crazy Joe"
    KOI           = "KOI"
    SVS           = "SVS"
    FOUNDRY       = "Foundry"
    CANYON_CLASH  = "Canyon Clash"
    CASTLE_BATTLE = "Castle Battle"
    OTHER         = "Other"

class LeaderboardOCR(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.db   = ProcessedImageDB()
        self.nick = NicknameResolver()
        self.sheet_mgr = SheetService("gspread-creds.json",
                                      "Whiteout Bear Trap Leaderboards")
        self.channel_id = DEFAULT_LEADERBOARD_CH

    # --------------------------------------------------
    @app_commands.command(
        name="upload_leaderboard",
        description="Admin‑only: upload screenshots og opdatér pinned scoreboard."
    )
    async def upload_leaderboard(
        self,
        interaction: discord.Interaction,
        event: EventType,
    ):
        # -------- permission ----------
        if not interaction.user.guild_permissions.administrator:
            await interaction.response.send_message("❌ Admin only.", ephemeral=True)
            return

        await interaction.response.send_message(
            "📂 Upload dine screenshot(s) nu (60 s)…", ephemeral=True
        )

        def _check(msg: discord.Message):
            return (
                msg.author.id == interaction.user.id
                and msg.channel.id == interaction.channel.id
                and msg.attachments
            )

        try:
            msg = await self.bot.wait_for("message", timeout=60, check=_check)
            images: List[bytes] = [await a.read() for a in msg.attachments]
            await msg.delete()
        except asyncio.TimeoutError:
            await interaction.followup.send("⏰ Tiden løb ud – prøv igen.", ephemeral=True)
            return

        if not images:
            await interaction.followup.send("Ingen billeder modtaget.", ephemeral=True)
            return

        await interaction.followup.send("🖼️ Behandler OCR…", ephemeral=True)
        event_name = event.value

        # -------- OCR ----------
        ocr_lines: List[str] = []
        for data in images:
            if self.db.is_processed(data):
                continue
            self.db.mark_processed(data)
            ocr_lines.extend(ocr_lines_from_image(data))

        if not ocr_lines:
            await interaction.followup.send("⚠️ Ingen brugbare OCR‑linjer.", ephemeral=True)
            return

        with open(OCR_DEBUG_LOG, "a", encoding="utf-8") as dbg:
            dbg.write(f"\n===== {datetime.utcnow()} / {interaction.user} =====\n")
            dbg.writelines(l + "\n" for l in ocr_lines)

        # -------- Sheet setup ----------
        ws         = self.sheet_mgr.worksheet(event_name, wide=True)
        header     = ws.row_values(1)
        col_header = datetime.utcnow().strftime("%Y-%m-%d %H:%M UTC")
        target_col = len(header) + 1
        ws.update_cell(1, target_col, col_header)

        existing: Dict[str, int] = {
            r[0].strip().lower(): idx
            for idx, r in enumerate(ws.get_all_values()[1:], start=2)
            if r and r[0].strip()
        }

        # -------- Parse OCR ----------
        raw_entries: List[Tuple[str, str, int]] = []
        for i, line in enumerate(ocr_lines):
            if "[PnK]" not in line:
                continue
            parsed = parse_tag_and_name(line)
            if not parsed:
                continue
            tag, raw_name = parsed
            scores = [
                s for off in range(-2, 8)
                if 0 <= i+off < len(ocr_lines)
                for s in [parse_damage_score(ocr_lines[i+off])]
                if s is not None
            ]
            if scores:
                raw_entries.append((raw_name, tag, max(scores)))

        if not raw_entries:
            await interaction.followup.send("⚠️ Ingen navn/score fundet.", ephemeral=True)
            return

        # -------- Konsolider ----------
        consolidated: Dict[str, Tuple[str, str, int]] = {}
        for nm, tg, sc in raw_entries:
            key = nm.lower()
            if key not in consolidated or sc > consolidated[key][2]:
                consolidated[key] = (nm, tg, sc)

        final = [(self.nick(nm), tg, sc) for nm, tg, sc in consolidated.values()]

        # -------- Upsert til sheet ----------
        for pname, ptag, sc in final:
            norm = pname.lower()
            if norm in existing:
                r = existing[norm]
                ws.update_cell(r, 1, pname)
                ws.update_cell(r, 2, ptag)
                ws.update_cell(r, target_col, str(sc))
            else:
                new = [""] * target_col
                new[0], new[1], new[target_col-1] = pname, ptag, str(sc)
                ws.append_row(new)
                existing[norm] = len(existing) + 2

        # -------- Byg embed ----------
        scoreboard = [
            (row[0], row[1], clean_int(row[target_col-1]))
            for row in ws.get_all_values()[1:]
            if len(row) >= target_col and row[0].strip()
        ]
        scoreboard.sort(key=lambda x: x[2], reverse=True)

        embed = discord.Embed(
            title=f"{event_name} Leaderboard ({col_header})",
            color=discord.Color.blue(),
        )
        chunk = 10
        for i in range(0, len(scoreboard), chunk):
            part = scoreboard[i:i+chunk]
            text = "\n".join(
                f"**{i+j+1}.** {n} (**{t}**) – *{s}*"
                for j, (n, t, s) in enumerate(part)
            )
            name = "Leaderboard" if i == 0 else f"Leaderboard ({i+1}-{i+len(part)})"
            embed.add_field(name=name, value=text, inline=False)

        # -------- Pin / update pin ----------
        chan = self.bot.get_channel(self.channel_id)
        pinned = load_json(PINNED_MESSAGES_FILE)
        key    = event_name.lower()
        ok     = False
        if chan:
            try:
                if pid := pinned.get(key):
                    try:
                        msg = await chan.fetch_message(pid)
                        await msg.edit(embed=embed, content="")
                        ok = True
                    except discord.NotFound:
                        raise ValueError("pin forsvundet")
                if not ok:
                    msg = await chan.send(embed=embed)
                    await msg.pin()
                    pinned[key] = msg.id
                    save_json(PINNED_MESSAGES_FILE, pinned)
                    ok = True
            except Exception as e:
                print("[pin] ", e)

        # -------- svar ----------
        resp = discord.Embed(title="Upload‑status", color=discord.Color.green())
        resp.description = (
            f"✅ {len(final)} rækker behandlet\n"
            f"🕒 Kolonne: **{col_header}**\n"
            f"📌 Pinned opdateret: **{ok}**"
        )
        await interaction.followup.send(embed=resp, ephemeral=True)

async def setup(bot: commands.Bot):
    await bot.add_cog(LeaderboardOCR(bot))
