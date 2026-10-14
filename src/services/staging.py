"""
立ち位置・移動・向き・ショット・カメラ・効果（キャラクターが画面の中を自由に動く演出）。

いつもの「左右の定位置に2人が立って話す」画面だけでなく、セリフの意味と前後の展開に合わせて、
キャラクターが歩いて近づく・離れる・追いかける・後ずさる・振り向く・中央に集まる・1人だけになる…
といった動きを付ける（寸劇・ストーリーのように場面が次々と変わって見えるように）。

仕組み:
  - 台本の各セリフの "stage"（Scene.stage）に、そのセリフでの立ち位置（cast）・ショット（shot）・
    カメラ（camera）・効果（fx）を書く。書かれていないキャラクター・項目は、前のセリフの状態を引き継ぐ。
  - plan_stages() が、動画の頭から順に「各キャラクターがどこに・どの大きさで・どちらを向いて立っているか」を
    たどり、セリフごとに「始まりの立ち位置 → 終わりの立ち位置」と動き方（歩く・走る・跳ぶ…）を決める
    （前のセリフの終わりの位置から動き出すので、急に別の場所へ瞬間移動しない）。
  - 黒板・イラスト・本の表紙などを画面中央に出すセリフでは、キャラクターは自動で左右の定位置に戻る
    （黒板に重ならないように）。
  - 実際の描画（1フレームずつの合成）は motion.build_motion_clip が、このモジュールの位置の計算を使って行う。

何も指定が無い（stage が空・自動の効果もオフ）動画では、従来どおり左右の定位置の画面になる。
"""
from __future__ import annotations

from dataclasses import dataclass, field, replace
from typing import Optional

from src.models import Project, Scene
from src.services.compositor import _center_ratio, _drawable_characters, CHARACTER_ORDER
from src.utils.asset_loader import load_character_config

# --- 台本で使える値 ---
POSITIONS: dict[str, float] = {
    "out_left": -0.3, "far_left": 0.1, "left": 0.2, "center_left": 0.36, "center": 0.5,
    "center_right": 0.64, "right": 0.8, "far_right": 0.9, "out_right": 1.3,
}
POSITION_LABELS = {
    "": "前のまま", "home": "いつもの立ち位置", "out_left": "画面の外（左）", "far_left": "左端", "left": "左",
    "center_left": "中央の左", "center": "中央", "center_right": "中央の右", "right": "右", "far_right": "右端",
    "out_right": "画面の外（右）",
}
SIZES: dict[str, float] = {"small": 0.72, "normal": 1.0, "large": 1.22}
SIZE_LABELS = {"": "前のまま", "small": "小さく（遠く）", "normal": "普通", "large": "大きく（近く）"}
FACINGS = {"left": -1, "right": 1}
FACE_LABELS = {"": "自動（相手の方を向く）", "left": "左を向く", "right": "右を向く"}
MOVES = ["", "walk", "run", "jump", "step_back", "approach", "retreat", "chase", "turn", "enter", "exit"]
MOVE_LABELS = {
    "": "自動（位置が変われば歩く）", "walk": "歩いて移動", "run": "走って移動", "jump": "跳んで移動",
    "step_back": "後ずさる（向きはそのまま下がる）", "approach": "相手に近づく", "retreat": "相手から離れる",
    "chase": "追いかける（走る）", "turn": "振り向く", "enter": "画面の外から入ってくる", "exit": "画面の外へ出ていく",
}
SHOTS = ["", "talk", "solo", "closeup", "group", "wide"]
SHOT_LABELS = {
    "": "前のまま", "talk": "いつもの2人の会話（定位置に戻る）", "solo": "話者1人だけ（中央・大きめ）",
    "closeup": "話者の顔のアップ", "group": "全員が中央に集まる", "wide": "引きの画（全員小さめ・場面が見える）",
}
CAMERAS = ["", "pan_left", "pan_right", "zoom_in", "zoom_out", "follow"]
CAMERA_LABELS = {
    "": "なし", "pan_left": "左へパン", "pan_right": "右へパン", "zoom_in": "ゆっくり寄る",
    "zoom_out": "寄った状態から引く", "follow": "話者を追う",
}
FX = ["", "focus_lines", "speed_lines", "flash", "gloom", "exclaim", "question", "sweat", "anger", "sparkle"]
FX_LABELS = {
    "": "なし", "focus_lines": "集中線", "speed_lines": "スピード線", "flash": "フラッシュ", "gloom": "どんより縦線",
    "exclaim": "「！」マーク", "question": "「？」マーク", "sweat": "汗マーク", "anger": "怒りマーク", "sparkle": "キラキラ",
}

# 表情から自動で付ける効果（Project.auto_fx。表情が変わった瞬間だけ）
AUTO_FX = {
    "surprised": "exclaim", "shock": "focus_lines", "panic": "sweat", "troubled": "sweat", "angry": "anger",
    "gloomy": "gloom", "think": "question", "idea": "sparkle", "excited": "sparkle",
}

# --- 動きの速さ（画面幅/秒） ---
WALK_SPEED = 0.55
RUN_SPEED = 1.5
STEP_BACK_SPEED = 0.22
MIN_MOVE_SECONDS = 0.35
MAX_MOVE_SECONDS = 1.6
JUMP_SECONDS = 0.55
APPROACH_GAP = 0.24     # 近づいたときの2人の間隔（画面幅に対する比率）
RETREAT_DISTANCE = 0.18
PARTNER_DELAY = 0.1     # 話者以外が動くときの、少し遅れて動き出す時間


@dataclass(frozen=True)
class Placement:
    x: float            # 立ち位置の中心（画面幅に対する比率。0〜1の外は画面の外）
    scale: float = 1.0  # いつもの大きさに対する倍率
    facing: int = 0     # 向き（左=-1、右=+1、0=素材のまま）

    @property
    def on_screen(self) -> bool:
        return -0.1 < self.x < 1.1


@dataclass
class CharTrack:
    """1セリフの間の、1人のキャラクターの立ち位置の変化。"""
    start: Placement
    end: Placement
    move: str = ""            # walk / run / jump / step_back / turn（位置が変わらなければ空）
    delay: float = 0.0
    duration: float = 0.0     # 移動にかける秒数

    def at(self, t: float) -> tuple[Placement, float]:
        """時刻 t の位置と、移動の進み具合（0〜1。歩く動きの上下などに使う）。"""
        if self.duration <= 0 or t <= self.delay:
            return (self.start if self.duration > 0 else self.end), (0.0 if self.duration > 0 else 1.0)
        p = min(1.0, (t - self.delay) / self.duration)
        e = p * p * (3 - 2 * p)
        return Placement(
            x=self.start.x + (self.end.x - self.start.x) * e,
            scale=self.start.scale + (self.end.scale - self.start.scale) * e,
            facing=self.end.facing,
        ), p


@dataclass
class StagePlan:
    """1セリフ分の演出（キャラクターごとの立ち位置の変化・ショット・カメラ・効果）。"""
    chars: dict[str, CharTrack] = field(default_factory=dict)
    shot: str = ""
    camera: str = ""
    fx: str = ""
    fx_target: str = ""   # 効果を付けるキャラクター（マーク系は頭の上に出す）

    @property
    def moving(self) -> bool:
        return any(track.duration > 0 for track in self.chars.values())

    @property
    def animated(self) -> bool:
        return self.moving or bool(self.camera) or bool(self.fx) or self.shot == "closeup"


def native_facing(character_key: str) -> int:
    """立ち絵の素材がもともと向いている向き（config/characters.json の "facing"。既定: つむぎは右、他は左）。"""
    value = str((load_character_config().get(character_key) or {}).get("facing") or "").strip()
    if value in FACINGS:
        return FACINGS[value]
    return 1 if character_key == "kasukabe_tsumugi" else -1


def home(character_key: str, resolution: tuple[int, int]) -> Placement:
    """いつもの立ち位置（左右の定位置）。向きは素材のまま。"""
    return Placement(x=_center_ratio(character_key, resolution), scale=1.0, facing=0)


def _parse_stage(stage) -> dict:
    return stage if isinstance(stage, dict) else {}


def has_stage(scene: Scene) -> bool:
    stage = _parse_stage(scene.stage)
    return bool(stage.get("shot") or stage.get("camera") or stage.get("fx") or stage.get("cast"))


def board_visible(scene: Scene) -> bool:
    """画面中央に黒板・イラスト・解説カード・本の表紙・資料を出しているセリフ（キャラクターは定位置に戻る）。"""
    return bool(scene.shows_board or scene.illustration_path or scene.note_text.strip()
                or scene.show_book_cover or scene.content_media_path)


def uses_staging(project: Project) -> bool:
    return any(has_stage(s) for s in project.scenes)


def _visible_keys(scene: Scene) -> list[str]:
    hidden = set(scene.render_hidden)
    return [k for k in CHARACTER_ORDER if k in _drawable_characters() and k not in hidden]


def _offscreen(x: float) -> float:
    return POSITIONS["out_left"] if x < 0.5 else POSITIONS["out_right"]


def _spread(keys: list[str], low: float, high: float, order: dict[str, float]) -> dict[str, float]:
    """keys を、今いる位置の並び順を保ったまま low〜high に等間隔で並べる。"""
    keys = sorted(keys, key=lambda k: order.get(k, 0.5))
    if len(keys) == 1:
        return {keys[0]: (low + high) / 2}
    step = (high - low) / (len(keys) - 1)
    return {k: low + step * i for i, k in enumerate(keys)}


def _auto_facing(key: str, x: float, others: dict[str, float]) -> int:
    """相手（一番近いキャラクター）の方を向く。1人なら画面の中央を向く。"""
    candidates = [ox for k, ox in others.items() if k != key and -0.05 < ox < 1.05]
    if candidates:
        target = min(candidates, key=lambda ox: abs(ox - x))
    else:
        target = 0.5
    if abs(target - x) < 1e-3:
        return 0
    return 1 if target > x else -1


MIN_GAP = 0.14  # 2人がこれより近いと重なって見えるので離す（画面幅に対する比率）


def _separate(target: dict[str, Placement], explicit: set[str]) -> None:
    """同じ場所に2人が重なったら、立ち位置を指定されていない方を少しずらす（両方指定なら台本どおり）。"""
    keys = [k for k, p in target.items() if p.on_screen]
    for i, a in enumerate(keys):
        for b in keys[i + 1:]:
            pa, pb = target[a], target[b]
            if abs(pa.x - pb.x) >= MIN_GAP or (a in explicit and b in explicit):
                continue
            mover, anchor = (b, pa) if a in explicit or b not in explicit else (a, pb)
            mp = target[mover]
            direction = 1 if mp.x >= anchor.x else -1
            if not 0.08 <= anchor.x + direction * 0.2 <= 0.92:
                direction = -direction
            target[mover] = replace(mp, x=anchor.x + direction * 0.2)


def _move_seconds(move: str, distance: float) -> float:
    if move == "jump":
        return JUMP_SECONDS
    speed = {"run": RUN_SPEED, "chase": RUN_SPEED, "step_back": STEP_BACK_SPEED}.get(move, WALK_SPEED)
    return max(MIN_MOVE_SECONDS, min(MAX_MOVE_SECONDS, distance / speed))


# ---------------------------------------------------------------------------
# 自動の演出（台本に stage が書かれていないブロックで、セリフの表情・流れから立ち位置を決める）
# ---------------------------------------------------------------------------

AUTO_GROUP_AFTER = 3      # 黒板の無い会話がこのセリフ数以上続くときは、2人が中央に寄って話す
AUTO_VARY_EVERY = 4       # 同じ構図がこのセリフ数続いたら、構図を変える


def _blocks(scenes: list[Scene]) -> list[list[int]]:
    blocks: list[list[int]] = []
    for i, scene in enumerate(scenes):
        prev = scenes[i - 1] if i else None
        same = prev is not None and prev.section == scene.section and prev.phase == scene.phase \
            and prev.slide_title == scene.slide_title
        if same and blocks:
            blocks[-1].append(i)
        else:
            blocks.append([i])
    return blocks


def _partner_of(scene: Scene, visible: list[str]) -> Optional[str]:
    others = [k for k in visible if k != scene.speaker]
    return others[0] if others else None


def auto_directions(project: Project) -> list[dict]:
    """台本に演出（stage）の無いブロックの各セリフに、セリフの意味と流れから演出を付ける（指定があるセリフは空）。

    方針（ランダムには動かさない。気持ちが動いたセリフで動かす）:
      - 黒板の無い会話が続くところでは、2人が中央に寄って向かい合って話す（いつも端に立ったままにしない）
      - 調子に乗る・はしゃぐ: 話者が中央へ走り出て大きく（躍り出る）
      - ショック・驚き: 後ずさる ／ 焦り: 相手と反対の端へ逃げる
      - 落ち込み・大泣き: 自分の側の端で小さくなる ／ ひらめき・立ち直り: 跳んで中央へ戻る
      - ツッコミ・怒り・ひそひそ: 相手に詰め寄る（近づく）
      - 場面転換（「3日後…」）のあと: 引きの画から始める
      - 同じ構図が長く続いたら、引き・寄りを入れ替える
      - 黒板・イラストを出すセリフは何もしない（自動で左右に戻る）
    """
    scenes = project.scenes
    result: list[dict] = [{} for _ in scenes]
    if not project.auto_stage or project.source_kind == "english":
        return result  # 英会話はレッスンの型が決まっているので、自動では動かさない
    for block in _blocks(scenes):
        if any(has_stage(scenes[i]) for i in block):
            continue  # 台本に演出が書かれているブロックは、台本どおり
        run = 0          # 黒板の無い会話が続いているセリフ数
        since_change = 0
        layout = "home"  # 今の構図: home（左右）/ group（中央で向かい合う）/ wide / edge（誰かが端・小さい）
        after_card = False
        prev: Optional[Scene] = None
        for i in block:
            scene = scenes[i]
            if scene.card_text.strip():
                after_card = True
                continue
            if board_visible(scene) or not scene.text.strip():
                run, layout, since_change = 0, "home", 0
                prev = scene
                continue
            visible = _visible_keys(scene)
            speaker = scene.speaker if scene.speaker in visible else ""
            partner = _partner_of(scene, visible)
            changed = prev is None or prev.speaker != scene.speaker or prev.expression != scene.expression
            expr = scene.expression
            remaining = 0
            for j in block[block.index(i):]:
                if scenes[j].card_text.strip() or board_visible(scenes[j]):
                    break
                remaining += 1
            stage: dict = {}
            if after_card:
                stage = {"shot": "wide"}
                layout = "wide"
            elif speaker and changed and layout != "solo-ish" and (
                    expr == "excited" or (expr == "happy" and scene.section == "intro")):
                stage = {"cast": [{"who": speaker, "pos": "center", "size": "large", "move": "run"}]}
                layout = "solo-ish"
            elif speaker and changed and expr in ("shock", "surprised") and layout != "edge":
                stage = {"cast": [{"who": speaker, "move": "step_back"}]}
            elif speaker and changed and expr == "panic":
                stage = {"cast": [{"who": speaker, "pos": "far_left" if _home_side(speaker, project) < 0 else "far_right",
                                   "move": "run"}]}
                layout = "edge"
            elif speaker and changed and expr in ("gloomy", "cry", "sad") and layout != "edge":
                stage = {"cast": [{"who": speaker, "pos": "far_left" if _home_side(speaker, project) < 0 else "far_right",
                                   "size": "small", "move": "walk"}]}
                layout = "edge"
            elif speaker and changed and expr == "idea" and layout == "edge":
                stage = {"cast": [{"who": speaker, "pos": "center", "size": "normal", "move": "jump"}]}
                layout = "solo-ish"
            elif speaker and partner and (scene.char_motion == "tsukkomi" or (changed and expr in ("angry", "whisper"))):
                stage = {"cast": [{"who": speaker, "move": "approach"}]}
                layout = "group"
            elif layout in ("home", "edge", "solo-ish", "wide") and remaining + run >= AUTO_GROUP_AFTER and \
                    since_change >= (0 if layout == "home" else 2):
                stage = {"shot": "group"}
                layout = "group"
            elif since_change >= AUTO_VARY_EVERY:
                stage = {"shot": "wide"} if layout == "group" else {"shot": "group"}
                layout = "wide" if layout == "group" else "group"
            if stage:
                result[i] = stage
                since_change = 0
            else:
                since_change += 1
            after_card = False
            run += 1
            prev = scene
    return result


def _home_side(character_key: str, project: Project) -> int:
    return -1 if home(character_key, project.resolution).x < 0.5 else 1


def plan_stages(project: Project) -> list[Optional[StagePlan]]:
    """各セリフの演出（StagePlan）。演出が要らないセリフ（いつもの画面のまま）は None。"""
    resolution = project.resolution
    scenes = project.scenes
    auto = auto_directions(project)
    staging = uses_staging(project) or any(auto)
    state: dict[str, Placement] = {}
    plans: list[Optional[StagePlan]] = []
    prev_scene: Optional[Scene] = None
    for idx, scene in enumerate(scenes):
        if scene.card_text.strip():
            plans.append(None)  # 場面転換テロップ（全画面の文字）の間は立ち位置を変えない
            continue
        visible = _visible_keys(scene)
        stage = (_parse_stage(scene.stage) or auto[idx]) if staging else {}
        # 前のセリフの終わりの状態（初めて出てくるキャラクターは、いつもの立ち位置）
        start = {k: state.get(k, home(k, resolution)) for k in set(visible) | set(state)}
        target: dict[str, Placement] = {}
        moves: dict[str, str] = {}

        if board_visible(scene) or not staging:
            for k in visible:
                target[k] = home(k, resolution)
        else:
            shot = str(stage.get("shot") or "")
            current_x = {k: start[k].x for k in visible}
            if shot == "talk":
                target = {k: home(k, resolution) for k in visible}
            elif shot == "solo":
                target = {k: replace(start[k], x=_offscreen(start[k].x)) for k in visible if k != scene.speaker}
                if scene.speaker in visible:
                    target[scene.speaker] = Placement(x=0.5, scale=SIZES["large"])
            elif shot == "group":
                xs = _spread(visible, 0.32 if len(visible) > 2 else 0.38, 0.68 if len(visible) > 2 else 0.62, current_x)
                target = {k: Placement(x=xs[k], scale=1.0) for k in visible}
            elif shot == "wide":
                xs = _spread(visible, 0.22, 0.78, current_x)
                target = {k: Placement(x=xs[k], scale=SIZES["small"]) for k in visible}
            else:
                target = {k: start[k] for k in visible}
            # キャラクターごとの指定（cast）
            for item in stage.get("cast") or []:
                if not isinstance(item, dict):
                    continue
                key = str(item.get("who") or "")
                if key not in start and key not in visible:
                    continue
                base = target.get(key, start.get(key, home(key, resolution)))
                pos = str(item.get("pos") or "")
                size = str(item.get("size") or "")
                face = str(item.get("face") or "")
                move = str(item.get("move") or "")
                x = base.x
                if pos == "home":
                    x = home(key, resolution).x
                elif pos in POSITIONS:
                    x = POSITIONS[pos]
                others = {k: p.x for k, p in target.items() if k != key}
                partner_x = min(others.values(), key=lambda ox: abs(ox - x)) if others else None
                if move == "approach" and partner_x is not None and not pos:
                    x = partner_x - APPROACH_GAP if x < partner_x else partner_x + APPROACH_GAP
                elif move == "retreat" and partner_x is not None and not pos:
                    x = x - RETREAT_DISTANCE if x < partner_x else x + RETREAT_DISTANCE
                elif move == "step_back" and partner_x is not None and not pos:
                    x = x - RETREAT_DISTANCE * 0.6 if x < partner_x else x + RETREAT_DISTANCE * 0.6
                elif move == "exit" and not pos:
                    x = _offscreen(x)
                elif move == "enter" and not start.get(key, base).on_screen and not pos:
                    x = home(key, resolution).x
                scale = SIZES.get(size, base.scale)
                facing = FACINGS.get(face, 0)
                target[key] = Placement(x=x, scale=scale, facing=facing)
                if move:
                    moves[key] = move
                if move == "enter" and start.get(key) is not None and start[key].on_screen:
                    # 画面にいたキャラクターを「入ってくる」にしたときは、外から入り直す
                    start[key] = replace(start[key], x=_offscreen(x))

        if staging:
            # 非表示になったキャラクターは画面の外へ出ていく（前のセリフで画面にいた場合）
            for k, p in start.items():
                if k not in visible and k not in target:
                    target[k] = replace(p, x=_offscreen(p.x)) if p.on_screen and prev_scene is not None else p
            # 新しく表示されたキャラクター（前は非表示）は、画面の外から入ってくる
            if prev_scene is not None:
                prev_visible = set(_visible_keys(prev_scene))
                for k in visible:
                    if k not in prev_visible and start[k].on_screen:
                        start[k] = replace(target.get(k, start[k]), x=_offscreen(target.get(k, start[k]).x))
        else:
            # 演出の指定が無い動画: 立ち位置は動かさない（登場・退場は従来の動き motion.entering_characters に任せる）
            target = {k: home(k, resolution) for k in visible}
            start = dict(target)
        if staging and not board_visible(scene):
            _separate(target, explicit={str(i.get("who")) for i in stage.get("cast") or []
                                        if isinstance(i, dict) and i.get("pos")})
        # 向き: 指定が無ければ相手の方を向く（いつもの定位置の画面では素材の向きのまま）
        positions = {k: p.x for k, p in target.items()}
        tracks: dict[str, CharTrack] = {}
        for k, end in target.items():
            begin = start.get(k, end)
            if staging and not board_visible(scene) and end.facing == 0 and end.on_screen:
                end = replace(end, facing=_auto_facing(k, end.x, positions))
            move = moves.get(k, "")
            distance = abs(end.x - begin.x)
            moving = distance > 0.01 or abs(end.scale - begin.scale) > 0.01
            if move == "turn" and not moving:
                tracks[k] = CharTrack(start=replace(begin, facing=-(end.facing or 1)), end=end, move="turn",
                                      duration=0.2, delay=0.1)
                continue
            if not moving:
                tracks[k] = CharTrack(start=end, end=end)
                continue
            move = move if move in ("run", "jump", "step_back", "chase") else ("run" if distance > 0.6 else "walk")
            tracks[k] = CharTrack(
                start=begin, end=end, move="run" if move == "chase" else move,
                delay=0.0 if k == scene.speaker else PARTNER_DELAY,
                duration=_move_seconds(move, distance),
            )
        plan = StagePlan(chars=tracks, shot=str(stage.get("shot") or ""), camera=str(stage.get("camera") or ""),
                         fx=str(stage.get("fx") or ""), fx_target=str(stage.get("fx_target") or scene.speaker))
        if plan.shot not in SHOTS:
            plan.shot = ""
        if plan.camera not in CAMERAS:
            plan.camera = ""
        if plan.fx not in FX:
            plan.fx = ""
        if not plan.fx and any(m == "chase" for m in moves.values()):
            plan.fx = "speed_lines"  # 追いかけっこにはスピード線
        if not plan.fx and project.auto_fx and scene.text.strip() and scene.speaker in visible and (
                prev_scene is None or prev_scene.speaker != scene.speaker or prev_scene.expression != scene.expression):
            plan.fx = AUTO_FX.get(scene.expression, "")
        # 何も変わらない（全員いつもの立ち位置のまま・効果も無い）セリフは、従来の画面で描く
        at_home = all(
            t.duration == 0 and abs(t.end.x - home(k, resolution).x) < 1e-3 and abs(t.end.scale - 1) < 1e-3
            and t.end.facing == 0 for k, t in tracks.items() if t.end.on_screen
        )
        plans.append(None if at_home and not plan.camera and not plan.fx and plan.shot != "closeup" else plan)
        state = {k: t.end for k, t in tracks.items()}
        prev_scene = scene
    return plans
