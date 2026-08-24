export class SpendGuard {
  constructor({ dailyBudget }) {
    this.dailyBudget = dailyBudget;
    this.day = this.currentDay();
    this.spent = 0;
  }

  currentDay() {
    return new Date().toISOString().slice(0, 10);
  }

  resetIfNeeded() {
    const today = this.currentDay();
    if (today !== this.day) {
      this.day = today;
      this.spent = 0;
    }
  }

  reserve(amount) {
    this.resetIfNeeded();
    if (!Number.isFinite(amount) || amount <= 0) throw new Error("Payment amount must be positive.");
    if (this.spent + amount > this.dailyBudget) {
      const error = new Error("Daily Mesh spending limit reached.");
      error.code = "daily_budget_exceeded";
      throw error;
    }
    this.spent += amount;
  }

  release(amount) {
    this.spent = Math.max(0, this.spent - amount);
  }
}

