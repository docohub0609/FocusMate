export type RepairStatus = {
  status: 'pending' | 'running' | 'submitted' | 'validating' | 'succeeded' | 'failed' | 'abstained' | 'interrupted' | 'exhausted';
  reason: string;
};

export const repairing = (repair?: RepairStatus | null) =>
  !!repair && ['pending', 'running', 'submitted', 'validating'].includes(repair.status);
