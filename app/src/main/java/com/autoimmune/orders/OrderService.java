package com.autoimmune.orders;

import java.math.BigDecimal;
import java.math.RoundingMode;
import java.util.List;

import org.springframework.stereotype.Service;

@Service
public class OrderService {

    static final BigDecimal DISCOUNT_THRESHOLD = new BigDecimal("1000.00");
    static final BigDecimal DISCOUNT_FACTOR = new BigDecimal("0.90");

    private final OrderRepository repository;

    public OrderService(OrderRepository repository) {
        this.repository = repository;
    }

    public List<Order> findAll() {
        return repository.findAll();
    }

    public Order get(long id) {
        return repository.findById(id).orElseThrow(() -> new OrderNotFoundException(id));
    }

    public BigDecimal total(long id) {
        BigDecimal sum = BigDecimal.ZERO;
        for (Item item : get(id).items()) {
            sum = sum.add(item.price().multiply(BigDecimal.valueOf(item.qty())));
        }
        return sum.setScale(2, RoundingMode.HALF_UP);
    }

    public BigDecimal discountedTotal(long id) {
        BigDecimal total = total(id);
        if (total.compareTo(DISCOUNT_THRESHOLD) >= 0) {
            return total.multiply(DISCOUNT_FACTOR).setScale(2, RoundingMode.HALF_UP);
        }
        return total;
    }

    public List<Order> searchByCustomer(String customer) {
        return repository.findAll().stream()
                .filter(o -> o.customer().equalsIgnoreCase(customer))
                .toList();
    }
}
