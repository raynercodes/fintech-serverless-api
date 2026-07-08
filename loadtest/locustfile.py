from locust import HttpUser, task, between
import random
import uuid


class FintechUser(HttpUser):
    # Wait 1-3 seconds between tasks — mimics realistic human pacing
    # rather than hammering the API as fast as physically possible
    wait_time = between(1, 3)

    def on_start(self):
        """
        Runs ONCE per simulated user, not per request. Each virtual
        user gets its own unique account — this spreads SQS messages
        across separate MessageGroupIds, so the worker can genuinely
        process them in parallel instead of bottlenecking on one
        shared FIFO group (which is what would happen if everyone
        used the shared /auth/demo account instead).
        """
        unique_id = uuid.uuid4().hex[:12]
        self.email = f"loadtest_{unique_id}@example.com"
        self.password = "LoadTest1!"
        self.account_id = f"acc_lt_{unique_id}"
        self.customer_id = f"cust_lt_{unique_id}"
        self.submitted_loan_ids = []

        self.client.post("/auth/register", json={
            "email": self.email,
            "password": self.password,
            "account_id": self.account_id,
            "customer_id": self.customer_id
        })

        response = self.client.post("/auth/login", json={
            "email": self.email,
            "password": self.password
        })

        if response.status_code == 200:
            token = response.json()["access_token"]
            self.headers = {"Authorization": f"Bearer {token}"}
        else:
            self.headers = {}

    @task(3)
    def submit_loan(self):
        # Random amount/credit_score/description each time — avoids
        # accidentally tripping the 90-second duplicate-detection
        # window between a user's own repeated submissions, which
        # would otherwise skew throughput numbers (a "duplicate" hit
        # returns the cached original instead of doing real work)
        payload = {
            "account_id": self.account_id,
            "customer_id": self.customer_id,
            "amount": round(random.uniform(500, 50000), 2),
            "type": "deposit",
            "credit_score": random.randint(300, 850),
            "description": f"Load test loan {uuid.uuid4().hex[:8]}"
        }
        response = self.client.post("/loans/", json=payload, headers=self.headers)
        if response.status_code == 201:
            self.submitted_loan_ids.append(response.json()["transaction_id"])

    @task(5)
    def get_loan(self):
        # "name=" groups these under one label in Locust's report,
        # instead of every unique loan_id showing up as its own row
        if self.submitted_loan_ids:
            loan_id = random.choice(self.submitted_loan_ids)
            self.client.get(f"/loans/{loan_id}", headers=self.headers, name="/loans/[loan_id]")

    @task(2)
    def get_loans_by_account(self):
        self.client.get(f"/loans/account/{self.account_id}", headers=self.headers, name="/loans/account/[account_id]")

    @task(1)
    def health_check(self):
        self.client.get("/health")