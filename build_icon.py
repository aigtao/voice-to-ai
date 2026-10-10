from PIL import Image, ImageDraw


size = 256
image = Image.new("RGBA", (size, size), (0, 0, 0, 0))
draw = ImageDraw.Draw(image)
draw.rounded_rectangle((12, 12, 244, 244), radius=56, fill="#147d68")
draw.rounded_rectangle((97, 45, 159, 151), radius=30, fill="#ffffff")
draw.arc((66, 91, 190, 188), start=0, end=180, fill="#ffffff", width=14)
draw.rounded_rectangle((120, 161, 136, 199), radius=8, fill="#ffffff")
draw.rounded_rectangle((91, 196, 165, 211), radius=7, fill="#ffffff")
image.save("voice-to-ai.ico", format="ICO", sizes=[(16, 16), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)])
image.save("voice-to-ai-icon.png")
