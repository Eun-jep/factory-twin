using FactoryTwin.Core;
using FactoryTwin.Core.Modbus;

// 사용법: Collector [host] [port] [폴링 간격 ms] [응답 제한 시간 ms]
string host = args.Length > 0 ? args[0] : "127.0.0.1";
int port = args.Length > 1 ? int.Parse(args[1]) : 5020;
int intervalMs = args.Length > 2 ? int.Parse(args[2]) : 500;
int timeoutMs = args.Length > 3 ? int.Parse(args[3]) : 1000;

using var cts = new CancellationTokenSource();
Console.CancelKeyPress += (_, e) => { e.Cancel = true; cts.Cancel(); };

Console.WriteLine($"[collector v0.1] 대상 {host}:{port}, 폴링 {intervalMs} ms, 제한 시간 {timeoutMs} ms (Ctrl+C로 종료)");

await using var client = new ModbusTcpClient(host, port, TimeSpan.FromMilliseconds(timeoutMs));
int ok = 0, failed = 0, reconnects = 0;
TimeSpan backoff = TimeSpan.FromMilliseconds(500);
TimeSpan maxBackoff = TimeSpan.FromSeconds(8);

try
{
    while (!cts.IsCancellationRequested)
    {
        // 연결이 없으면 접속한다. 실패하면 대기 시간을 두 배씩 늘린다(최대 8초).
        if (!client.IsConnected)
        {
            try
            {
                await client.ConnectAsync(cts.Token);
                if (reconnects > 0 || failed > 0) Console.WriteLine("[collector] 다시 연결됨");
                reconnects++;
                backoff = TimeSpan.FromMilliseconds(500);
            }
            catch (Exception ex) when (ex is not OperationCanceledException)
            {
                Console.WriteLine($"[collector] 연결 실패: {ex.Message} -> {backoff.TotalSeconds:F1}초 후 재시도");
                await Task.Delay(backoff, cts.Token);
                backoff = TimeSpan.FromTicks(Math.Min(backoff.Ticks * 2, maxBackoff.Ticks));
                continue;
            }
        }

        try
        {
            ushort[] regs = await client.ReadHoldingRegistersAsync(
                unitId: 1, RegisterMap.StartAddress, RegisterMap.Count, cts.Token);
            var snap = RegisterMap.Parse(regs, DateTimeOffset.Now);
            ok++;
            Console.WriteLine(
                $"{snap.Timestamp:HH:mm:ss.fff} 관절1 {snap.Joint1Deg,5:F1}° 관절2 {snap.Joint2Deg,5:F1}° " +
                $"집게 {(snap.GripperClosed ? "닫힘" : "열림")} 컨베이어 {snap.ConveyorSpeedMmPerSec} mm/s " +
                $"온도 {snap.TemperatureC:F1}℃ [{snap.Status}]");
        }
        catch (Exception ex) when (ex is TimeoutException or IOException or ModbusProtocolException
                                      or ModbusExceptionResponse or FormatException)
        {
            // 어떤 오류든 연결 상태를 믿을 수 없으니 연결을 닫고 처음부터 다시 접속한다.
            failed++;
            Console.WriteLine($"[collector] 읽기 실패 ({ex.GetType().Name}): {ex.Message}");
            await client.DisposeAsync();
        }

        await Task.Delay(intervalMs, cts.Token);
    }
}
catch (OperationCanceledException)
{
    // 정상 종료
}

Console.WriteLine($"[collector] 종료. 성공 {ok}회, 실패 {failed}회, 연결 {reconnects}회");
