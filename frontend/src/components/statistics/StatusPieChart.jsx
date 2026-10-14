import { PieChart, Pie, Cell, ResponsiveContainer, Tooltip, Legend } from "recharts";

const STATUS_COLORS = {
  success: "#10B981",
  failed: "#EF4444",
  running: "#3B82F6",
  queued: "#9CA3AF",
};

const STATUS_LABELS = {
  success: "Succès",
  failed: "Échoué",
  running: "En cours",
  queued: "En attente",
};

const CustomTooltip = ({ active, payload }) => {
  if (!active || !payload?.length) return null;
  const { name, value } = payload[0];
  return (
    <div className="bg-white border border-gray-200 rounded-lg shadow-lg px-4 py-2 text-sm">
      <p style={{ color: payload[0].payload.fill }}>
        {STATUS_LABELS[name] || name}: {value}
      </p>
    </div>
  );
};

export default function StatusPieChart({ data = {} }) {
  const chartData = Object.entries(data)
    .filter(([, count]) => count > 0)
    .map(([status, count]) => ({
      name: status,
      value: count,
      fill: STATUS_COLORS[status] || "#9CA3AF",
    }));

  if (chartData.length === 0) {
    return (
      <div className="flex items-center justify-center h-64 text-gray-400 text-sm">
        Aucune donnée
      </div>
    );
  }

  return (
    <ResponsiveContainer width="100%" height={300}>
      <PieChart>
        <Pie
          data={chartData}
          cx="50%"
          cy="50%"
          innerRadius={60}
          outerRadius={100}
          paddingAngle={2}
          dataKey="value"
        >
          {chartData.map((entry, index) => (
            <Cell key={`cell-${index}`} fill={entry.fill} />
          ))}
        </Pie>
        <Tooltip content={<CustomTooltip />} />
        <Legend
          formatter={(value) => STATUS_LABELS[value] || value}
          wrapperStyle={{ fontSize: 13 }}
          iconType="circle"
          iconSize={10}
        />
      </PieChart>
    </ResponsiveContainer>
  );
}
