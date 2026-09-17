from PIL import Image, ImageDraw, ImageFont

S = 2
W, H = 1376, 578
SURFACE = (26, 26, 25)
INK = (255, 255, 255)
MUTED = (158, 158, 153)
RULE = (58, 58, 55)
ORANGE = (217, 89, 38)
BLUE = (57, 135, 229)
GOLD = (198, 156, 62)
PCB = (28, 74, 48)
BLACK = (20, 20, 20)
WHITE = (250, 250, 248)
BEAK = (232, 154, 40)
SANS_B = "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf"
SANS = "/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf"
MONO = "/usr/share/fonts/truetype/liberation/LiberationMono-Regular.ttf"


def f(p, pt):
    return ImageFont.truetype(p, pt * S)


im = Image.new("RGB", (W * S, H * S), SURFACE)
d = ImageDraw.Draw(im)


def B(xy, **k):
    d.rectangle([c * S for c in xy], **k)


def P(p, **k):
    d.polygon([(x * S, y * S) for x, y in p], **k)


def E(xy, **k):
    d.ellipse([c * S for c in xy], **k)


def T(xy, s, fnt, fill):
    d.text((xy[0] * S, xy[1] * S), s, font=fnt, fill=fill)


def w_(s, fnt):
    return d.textlength(s, font=fnt) / S


# ---------------- left column ----------------
T((72, 92), "AMD INSTINCT MI300X  ·  gfx942  ·  CDNA 3", f(MONO, 14), MUTED)
hl = f(SANS_B, 41)
for i, line in enumerate(["What the matrix cores", "execute, and what they don't"]):
    assert w_(line, hl) <= 560, (line, w_(line, hl))
    T((72, 136 + i * 52), line, hl, INK)
T((72, 256), "304 CUs · 191.69 GiB HBM3 · one $1.99/hr card", f(SANS, 19), MUTED)


def chip(x, y, label, num, unit, colour):
    B((x, y, x + 4, y + 54), fill=colour)
    T((x + 16, y), label, f(MONO, 13), MUTED)
    nf = f(SANS_B, 31)
    T((x + 16, y + 19), num, nf, INK)
    T((x + 16 + w_(num, nf) + 7, y + 32), unit, f(SANS, 15), MUTED)


chip(72, 328, "fp8 e4m3fnuz", "1.77", "x bf16", ORANGE)
chip(268, 328, "int8", "0.69", "x bf16", BLUE)
B((72, 424, 560, 425), fill=RULE)
T((72, 442), "Measured on the card, 2026-09-16", f(MONO, 13), MUTED)

# ---------------- accelerator card ----------------
X0, Y0, CW, CH, SK = 684, 232, 430, 128, 32
P([(X0, Y0), (X0 + CW, Y0), (X0 + CW + SK, Y0 - SK), (X0 + SK, Y0 - SK)], fill=(54, 54, 52))
B((X0, Y0, X0 + CW, Y0 + CH), fill=(36, 36, 35))
P([(X0 + SK, Y0 - SK), (X0 + CW + SK, Y0 - SK), (X0 + CW + SK, Y0 - SK + 7), (X0 + SK, Y0 - SK + 7)], fill=ORANGE)
for i in range(28):
    x = X0 + 26 + i * 15
    if x + 5 < X0 + CW - 16:
        B((x, Y0 + 18, x + 5, Y0 + CH - 16), fill=(62, 62, 60))
for x0 in (X0 + 64, X0 + 166):
    P(
        [(x0, Y0 - 4), (x0 + 74, Y0 - 4), (x0 + 74 + SK - 10, Y0 - SK + 8), (x0 + SK - 10, Y0 - SK + 8)],
        fill=(76, 76, 73),
    )
B((X0 - 4, Y0 + CH, X0 + CW + 4, Y0 + CH + 12), fill=PCB)
for i in range(24):
    x = X0 + 42 + i * 16
    if x + 9 < X0 + CW - 18:
        B((x, Y0 + CH + 12, x + 9, Y0 + CH + 32), fill=GOLD)
B((X0 - 22, Y0 - SK - 6, X0 - 4, Y0 + CH + 30), fill=(70, 70, 67))
for i in range(4):
    B((X0 - 19, Y0 - 10 + i * 30, X0 - 7, Y0 + 8 + i * 30), fill=(30, 30, 29))

# ---------------- Tux: beside the card, left flipper up on it ----------------
tx, ty, s = 1206, 352, 1.22


def sx(v):
    return tx + v * s


def sy(v):
    return ty + v * s


E((sx(-70), sy(88), sx(70), sy(110)), fill=(20, 20, 19))  # contact shadow
# raised flipper: tapered blade with a rounded tip, lying across the card's fins
P([(sx(-38), sy(-46)), (sx(-52), sy(-4)), (1082, 312), (1070, 286)], fill=BLACK)
E((1058, 280, 1092, 316), fill=BLACK)  # rounded flipper tip
E((sx(42), sy(-16), sx(66), sy(44)), fill=BLACK)  # right flipper, hanging
E((sx(-58), sy(-58), sx(58), sy(86)), fill=BLACK)  # body
E((sx(-41), sy(-30), sx(41), sy(78)), fill=WHITE)  # belly
E((sx(-45), sy(-124), sx(45), sy(-34)), fill=BLACK)  # head
E((sx(-28), sy(-80), sx(28), sy(-34)), fill=WHITE)  # face
for ex in (-22, 4):
    E((sx(ex), sy(-97), sx(ex + 18), sy(-75)), fill=WHITE)
for ex in (-15, 9):
    E((sx(ex), sy(-91), sx(ex + 8), sy(-81)), fill=(16, 16, 16))
P([(sx(-14), sy(-74)), (sx(14), sy(-74)), (sx(0), sy(-56))], fill=BEAK)  # beak
for fx in (-34, 8):  # feet
    P([(sx(fx), sy(78)), (sx(fx + 40), sy(70)), (sx(fx + 34), sy(96)), (sx(fx - 6), sy(96))], fill=BEAK)

im.resize((W, H), Image.LANCZOS).save("/tmp/cover-art.png")
print("ok")
