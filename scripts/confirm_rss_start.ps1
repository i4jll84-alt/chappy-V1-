param([Parameter(Mandatory=$true)][string]$WorkbookPath)
Add-Type -AssemblyName System.Windows.Forms
Add-Type -AssemblyName System.Drawing
$form=New-Object System.Windows.Forms.Form
$form.Text='RSS開始前の接続確認（実発注なし）'
$form.Size=New-Object System.Drawing.Size(700,500)
$form.StartPosition='CenterScreen'
$label=New-Object System.Windows.Forms.Label
$label.SetBounds(20,15,650,65)
$label.Text='Excelで「未接続」をクリックして「接続中」にしてください。発注は「発注不可」のままです。接続表示はご本人が確認します。セルが読めるだけでは接続・鮮度の証明になりません。'
$form.Controls.Add($label)
$checks=@()
$items=@('マーケットスピードIIにログインしました','ExcelのRSSが「接続中」、発注は「発注不可」です','場中は株価の時刻が進むことを確認しました（寄り前・昼休み・引け後は更新未検証と理解しました）')
for($i=0;$i -lt $items.Count;$i++){
 $box=New-Object System.Windows.Forms.CheckBox
 $box.SetBounds(20,90+($i*45),650,40)
 $box.Text=$items[$i]
 $form.Controls.Add($box)
 $checks+=$box
}
$remote=New-Object System.Windows.Forms.Button
$remote.SetBounds(20,235,290,35)
$remote.Text='スマホ遠隔操作の設定ページ'
$remote.Add_Click({Start-Process 'https://remotedesktop.google.com/access'})
$form.Controls.Add($remote)
$help=New-Object System.Windows.Forms.Label
$help.SetBounds(20,280,650,65)
$help.Text='遠隔操作はGoogleへのログイン・ホスト設定・PIN設定が必要です。PINはチャットに書かないでください。スマホから同じGoogleアカウントで接続します。PCの電源と通信が必要です。'
$form.Controls.Add($help)
$start=New-Object System.Windows.Forms.Button
$start.SetBounds(350,365,300,40)
$start.Text='確認済み：セル読取を検査して開始'
$start.Enabled=$false
$form.Controls.Add($start)
$cancel=New-Object System.Windows.Forms.Button
$cancel.SetBounds(20,365,160,40)
$cancel.Text='開始せず閉じる'
$cancel.Add_Click({$form.DialogResult=[System.Windows.Forms.DialogResult]::Cancel;$form.Close()})
$form.Controls.Add($cancel)
foreach($box in $checks){$box.Add_CheckedChanged({$start.Enabled=(@($checks | Where-Object {-not $_.Checked}).Count -eq 0)})}
$start.Add_Click({$form.DialogResult=[System.Windows.Forms.DialogResult]::OK;$form.Close()})
$result=$form.ShowDialog()
$form.Dispose()
return ($result -eq [System.Windows.Forms.DialogResult]::OK)