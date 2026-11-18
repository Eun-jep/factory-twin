using System.Buffers.Binary;
using System.Net.Sockets;

namespace FactoryTwin.Core.Modbus;

/// <summary>설비가 예외 코드로 응답했을 때 (예: 범위 밖 주소).</summary>
public sealed class ModbusExceptionResponse(byte functionCode, byte exceptionCode)
    : Exception($"Modbus 예외 응답: 기능 0x{functionCode:X2}, 예외 코드 0x{exceptionCode:X2}")
{
    public byte ExceptionCode { get; } = exceptionCode;
}

/// <summary>응답이 규격에 맞지 않을 때 (깨진 패킷, 다른 트랜잭션 번호 등).</summary>
public sealed class ModbusProtocolException(string message) : Exception(message);

/// <summary>
/// 라이브러리 없이 소켓으로 구현한 최소 Modbus TCP 클라이언트.
/// 요청/응답 프레임(MBAP 헤더 + PDU)을 직접 만들고 검증한다.
/// 연결 관리(재접속)는 호출하는 쪽의 책임이고, 이 클래스는 오류를 예외로 알려 준다.
/// </summary>
public sealed class ModbusTcpClient : IAsyncDisposable
{
    private const int MbapHeaderLength = 7;
    private const byte ReadHoldingRegisters = 0x03;

    private readonly string _host;
    private readonly int _port;
    private readonly TimeSpan _timeout;
    private TcpClient? _tcp;
    private NetworkStream? _stream;
    private ushort _transactionId;

    public ModbusTcpClient(string host, int port, TimeSpan timeout)
    {
        _host = host;
        _port = port;
        _timeout = timeout;
    }

    public bool IsConnected => _tcp?.Connected == true;

    public async Task ConnectAsync(CancellationToken ct)
    {
        await DisposeAsync();
        var tcp = new TcpClient { NoDelay = true };
        try
        {
            using var cts = CancellationTokenSource.CreateLinkedTokenSource(ct);
            cts.CancelAfter(_timeout);
            await tcp.ConnectAsync(_host, _port, cts.Token);
        }
        catch (OperationCanceledException) when (!ct.IsCancellationRequested)
        {
            tcp.Dispose();
            throw new TimeoutException($"연결 시간 초과 ({_timeout.TotalMilliseconds:F0} ms)");
        }
        catch
        {
            tcp.Dispose();
            throw;
        }
        _tcp = tcp;
        _stream = tcp.GetStream();
    }

    /// <summary>기능 0x03: Holding Register 여러 개 읽기.</summary>
    public async Task<ushort[]> ReadHoldingRegistersAsync(
        byte unitId, ushort address, ushort quantity, CancellationToken ct)
    {
        if (_stream is null) throw new InvalidOperationException("연결되지 않았습니다.");
        if (quantity is < 1 or > 125) throw new ArgumentOutOfRangeException(nameof(quantity));

        using var cts = CancellationTokenSource.CreateLinkedTokenSource(ct);
        cts.CancelAfter(_timeout);

        try
        {
            ushort tid = unchecked(++_transactionId);
            await _stream.WriteAsync(BuildReadRequest(tid, unitId, address, quantity), cts.Token);

            // 1) MBAP 헤더 7바이트
            var header = new byte[MbapHeaderLength];
            await ReadExactAsync(header, cts.Token);
            ushort rTid = BinaryPrimitives.ReadUInt16BigEndian(header.AsSpan(0, 2));
            ushort protocol = BinaryPrimitives.ReadUInt16BigEndian(header.AsSpan(2, 2));
            ushort length = BinaryPrimitives.ReadUInt16BigEndian(header.AsSpan(4, 2));

            if (protocol != 0)
                throw new ModbusProtocolException($"프로토콜 ID가 0이 아닙니다: {protocol}");
            if (length < 2 || length > 253)
                throw new ModbusProtocolException($"길이 필드가 비정상입니다: {length}");
            if (rTid != tid)
                throw new ModbusProtocolException($"트랜잭션 번호 불일치: 요청 {tid}, 응답 {rTid}");

            // 2) PDU (길이 필드는 unit id 1바이트를 포함한다)
            var pdu = new byte[length - 1];
            await ReadExactAsync(pdu, cts.Token);
            return ParseReadResponse(pdu, quantity);
        }
        catch (OperationCanceledException) when (!ct.IsCancellationRequested)
        {
            throw new TimeoutException($"응답 시간 초과 ({_timeout.TotalMilliseconds:F0} ms)");
        }
    }

    internal static byte[] BuildReadRequest(ushort tid, byte unitId, ushort address, ushort quantity)
    {
        var frame = new byte[12];
        BinaryPrimitives.WriteUInt16BigEndian(frame.AsSpan(0, 2), tid);
        BinaryPrimitives.WriteUInt16BigEndian(frame.AsSpan(2, 2), 0);      // protocol id
        BinaryPrimitives.WriteUInt16BigEndian(frame.AsSpan(4, 2), 6);      // unit id + PDU(5)
        frame[6] = unitId;
        frame[7] = ReadHoldingRegisters;
        BinaryPrimitives.WriteUInt16BigEndian(frame.AsSpan(8, 2), address);
        BinaryPrimitives.WriteUInt16BigEndian(frame.AsSpan(10, 2), quantity);
        return frame;
    }

    internal static ushort[] ParseReadResponse(byte[] pdu, ushort expectedQuantity)
    {
        if (pdu.Length < 2) throw new ModbusProtocolException("PDU가 너무 짧습니다.");

        if (pdu[0] == (ReadHoldingRegisters | 0x80))
            throw new ModbusExceptionResponse(ReadHoldingRegisters, pdu[1]);
        if (pdu[0] != ReadHoldingRegisters)
            throw new ModbusProtocolException($"예상하지 못한 기능 코드: 0x{pdu[0]:X2}");

        int byteCount = pdu[1];
        if (byteCount != expectedQuantity * 2 || pdu.Length != 2 + byteCount)
            throw new ModbusProtocolException(
                $"바이트 수 불일치: 필드 {byteCount}, 기대 {expectedQuantity * 2}, 실제 PDU {pdu.Length}");

        var values = new ushort[expectedQuantity];
        for (int i = 0; i < values.Length; i++)
            values[i] = BinaryPrimitives.ReadUInt16BigEndian(pdu.AsSpan(2 + i * 2, 2));
        return values;
    }

    private async Task ReadExactAsync(byte[] buffer, CancellationToken ct)
    {
        int read = 0;
        while (read < buffer.Length)
        {
            int n = await _stream!.ReadAsync(buffer.AsMemory(read), ct);
            if (n == 0) throw new IOException("상대가 연결을 닫았습니다.");
            read += n;
        }
    }

    public ValueTask DisposeAsync()
    {
        _stream?.Dispose();
        _tcp?.Dispose();
        _stream = null;
        _tcp = null;
        return ValueTask.CompletedTask;
    }
}
