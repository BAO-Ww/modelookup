; 型号查询 安装程序 —— 简体中文「部分翻译」覆盖层
; 用法（在 .iss 里）:
;   [Languages]
;   Name: "chinese"; MessagesFile: "compiler:Default.isl,ChineseSimplified.isl"
; 只覆盖看得见的界面文案，其余沿用 Default.isl 的英文兜底。
; （不引入第三方完整翻译包，避免供应链风险）

[LangOptions]
LanguageName=简体中文
LanguageID=$0804
LanguageCodePage=936

[Messages]
SetupAppTitle=安装程序
SetupWindowTitle=安装 %1
UninstallAppFullTitle=卸载 %1
UninstallAppTitle=卸载

ButtonBack=< 上一步(&B)
ButtonNext=下一步(&N) >
ButtonInstall=安装(&I)
ButtonCancel=取消
ButtonYes=是(&Y)
ButtonNo=否(&N)
ButtonFinish=完成(&F)
ButtonBrowse=浏览(&B)…

WelcomeLabel1=欢迎使用 [name] 安装向导
WelcomeLabel2=即将在你的电脑上安装 [name/ver]。%n%n这是一个独立窗口运行的小软件，不需要浏览器；装完会自动创建桌面和开始菜单快捷方式，随时可以在「设置 → 应用」里卸载。%n%n建议先关掉其它正在运行的程序再继续。

SelectDirDesc=安装位置
SelectDirLabel3=安装程序会把 [name] 装到下面的文件夹里（只装给当前用户，不需要管理员权限）。
SelectDirBrowseLabel=点「下一步」继续；想换位置就点「浏览」。

SelectTasksDesc=附加任务
SelectTasksLabel2=请选择安装 [name] 时要顺便做的事，然后点「下一步」。

SelectStartMenuFolderDesc=开始菜单文件夹
SelectStartMenuFolderLabel3=安装程序会在下面的开始菜单文件夹里创建快捷方式。
SelectStartMenuFolderBrowseLabel=点「下一步」继续；想换文件夹就点「浏览」。

ReadyLabel1=准备就绪，可以开始安装
ReadyLabel2a=点「安装」开始安装；想改点什么就点「上一步」。
ReadyLabel2b=点「安装」开始安装。
PreparingDesc=安装程序正在准备安装 [name]，请稍候。
InstallingLabel=正在安装 [name]，请稍候…

FinishedHeadingLabel=安装完成
FinishedLabelNoIcons=[name] 已经装好了。
FinishedLabel=[name] 已经装好了。桌面和开始菜单里都能找到「型号查询」。
ClickFinish=点「完成」结束安装，然后就可以用了。

ConfirmUninstall=确实要卸载 %1 及其所有组件吗？
UninstallStatusLabel=正在从你的电脑上卸载 %1，请稍候…
UninstalledAll=%1 已成功卸载。
UninstalledMost=%1 卸载完成。%n%n有少数文件删不掉，可以手动清理。
UninstalledAndNeedsRestart=要完成 %1 的卸载，必须重启电脑。%n%n现在就重启吗？

StatusExtractFiles=正在解压文件…
StatusCreateIcons=正在创建快捷方式…
StatusCreateRegistryEntries=正在写注册表项…
StatusRegisterFiles=正在注册文件…
StatusSavingUninstall=正在保存卸载信息…
StatusRunProgram=正在收尾…

