namespace FactoryTwin.Core;

public enum EquipmentStatus
{
    Normal = 0,
    Warning = 1,
    Fault = 2,
}

/// <summary>한 번 읽은 설비 상태. 레지스터 값을 사람이 읽을 수 있는 단위로 바꾼 것이다.</summary>
public sealed record EquipmentSnapshot(
    DateTimeOffset Timestamp,
    double Joint1Deg,
    double Joint2Deg,
    bool GripperClosed,
    int ConveyorSpeedMmPerSec,
    double TemperatureC,
    EquipmentStatus Status);

/// <summary>시뮬레이터(simulator/modbus_sim.py)의 레지스터 맵과 같아야 한다.</summary>
public static class RegisterMap
{
    public const string Version = "0.1";
    public const ushort StartAddress = 0;
    public const ushort Count = 6;

    public static EquipmentSnapshot Parse(ushort[] r, DateTimeOffset timestamp)
    {
        if (r.Length != Count)
            throw new ArgumentException($"레지스터 {Count}개가 필요합니다. 받은 개수: {r.Length}");

        return new EquipmentSnapshot(
            timestamp,
            Joint1Deg: r[0] / 10.0,
            Joint2Deg: r[1] / 10.0,
            GripperClosed: r[2] != 0,
            ConveyorSpeedMmPerSec: r[3],
            TemperatureC: r[4] / 10.0,
            Status: Enum.IsDefined(typeof(EquipmentStatus), (int)r[5])
                ? (EquipmentStatus)r[5]
                : throw new FormatException($"알 수 없는 상태 값: {r[5]}"));
    }
}
