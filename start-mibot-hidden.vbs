' Lanzador oculto de Mibot.
' Arranca el watchdog run-forever.ps1 (bot headless 24/7 con auto-reinicio)
' SIN mostrar ninguna ventana de consola. Pensado para la carpeta Startup de
' Windows: se ejecuta solo cada vez que inicias sesion.
'
' El lock data\bot.lock impide que se levante una segunda instancia, asi que es
' seguro aunque tambien corras el bot a mano.
Set sh = CreateObject("WScript.Shell")
sh.CurrentDirectory = "C:\Users\lauta\Mibot"
sh.Run "powershell.exe -ExecutionPolicy Bypass -NoProfile -WindowStyle Hidden -File ""C:\Users\lauta\Mibot\run-forever.ps1""", 0, False
