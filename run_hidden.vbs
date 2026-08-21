' run_hidden.vbs — executa o .bat passado como argumento SEM abrir janela.
' Usado pelas tarefas agendadas do MercadoBarato pra a coleta rodar invisível
' (nao pisca CMD enquanto voce usa o PC). Uso: wscript run_hidden.vbs "C:\...\algo.bat"
Set sh = CreateObject("WScript.Shell")
sh.Run "cmd /c """ & WScript.Arguments(0) & """", 0, True
